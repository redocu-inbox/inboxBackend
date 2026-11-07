"""Message list, detail, and a manual mailbox sync.

The JSON keys are frozen for the frontend. from_name, from_email, is_read, and
body_text are filled from Graph when a sync stores them, and stay null when
Graph omits them. body_text is plain text, never raw HTML. priority_score stays
null until a later phase writes it. priority_reasons is empty until then.
suggested_form is null until a later phase. draft stays null.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, nulls_last
from sqlmodel import Session, select

from app.api.deps import get_arq, get_current_user, get_session
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.user import User
from app.services.mailboxes import mark_sync_enqueue_failed, request_mailbox_sync

router = APIRouter(tags=["messages"])


class MessageResponse(BaseModel):
    id: UUID
    subject: str | None
    body_preview: str | None
    from_name: str | None = None
    from_email: str | None = None
    received_at: datetime
    is_read: bool | None = None
    conversation_id: str | None = None
    priority_score: float | None = None
    priority_band: str | None = None
    priority_reasons: list[Any] = Field(default_factory=list)
    suggested_form: Any | None = None


class MessageDetailResponse(MessageResponse):
    body_text: str | None = None
    draft: Any | None = None


class SyncResponse(BaseModel):
    enqueued: bool
    sync_status: Literal["idle", "pending", "running", "failed"]


def _visible_message_filter(user_id: UUID):
    """Messages on this user's active mailbox. Disconnected mailboxes stay in the database."""
    active_ids = select(ConnectedAccount.id).where(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.disconnected_at.is_(None),
    )
    return Message.user_id == user_id, Message.connected_account_id.in_(active_ids)


def _message_response(row: Message) -> MessageResponse:
    return MessageResponse(
        id=row.id,
        subject=row.subject,
        body_preview=row.body_preview,
        from_name=row.from_name,
        from_email=row.from_email,
        received_at=row.received_at,
        is_read=row.is_read,
        priority_score=row.priority_score,
    )


def _order_by(sort: Literal["received_at", "priority"]):
    if sort == "priority":
        # Null scores sort after every number, then newest first.
        return (
            nulls_last(Message.priority_score.desc()),
            Message.received_at.desc(),
        )
    return (Message.received_at.desc(),)


@router.get("/messages")
def list_messages(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: Literal["received_at", "priority"] = Query(default="received_at"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[MessageResponse]:
    """List this user's active-mailbox messages. X-Total-Count ignores limit and offset."""
    visible = _visible_message_filter(user.id)
    total = session.scalar(select(func.count(Message.id)).where(*visible))
    response.headers["X-Total-Count"] = str(total or 0)
    rows = session.exec(
        select(Message)
        .where(*visible)
        .order_by(*_order_by(sort))
        .offset(offset)
        .limit(limit)
    ).all()
    return [_message_response(row) for row in rows]


@router.get("/messages/{message_id}")
def read_message(
    message_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse:
    row = session.get(Message, message_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    account = session.get(ConnectedAccount, row.connected_account_id)
    if account is None or account.user_id != user.id or account.disconnected_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    item = _message_response(row)
    return MessageDetailResponse(**item.model_dump(), body_text=row.body_text, draft=None)


@router.post("/messages/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_messages(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SyncResponse:
    """Enqueue sync_mailbox_delta for the active mailbox, or report the job already in flight.

    `enqueued` is true only when this call took a new generation. A 202 with
    `enqueued` false means a pending or running job is still the current one.
    Queuing does not set last_successful_at. A queue outage is 503 and the row
    is failed, not idle.
    """
    requested = request_mailbox_sync(session, user.id)
    if requested is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mailbox is connected.",
        )
    if requested.enqueued:
        try:
            await get_arq(request).enqueue_job(
                "sync_mailbox_delta",
                str(requested.account_id),
                requested.generation,
            )
        except Exception as exc:
            mark_sync_enqueue_failed(session, requested.account_id, requested.generation)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Mailbox sync could not be queued.",
            ) from exc
    return SyncResponse(enqueued=requested.enqueued, sync_status=requested.sync_status)
