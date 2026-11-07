"""Message list, detail, and a manual mailbox sync.

The JSON keys are frozen for the frontend. Sender, read state, conversation id,
and priority band are null until phase 6 stores them. priority_score is the
stored column, which stays null until phase 6 writes it. priority_reasons is
empty until phase 6. suggested_form is null until phase 10. On the detail
route, body_text and draft are null until phase 7.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, nulls_last
from sqlmodel import Session, select

from app.api.deps import get_arq, get_current_user, get_session
from app.api.users import latest_connected_account
from app.models.message import Message
from app.models.user import User

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


def _message_response(row: Message) -> MessageResponse:
    return MessageResponse(
        id=row.id,
        subject=row.subject,
        body_preview=row.body_preview,
        received_at=row.received_at,
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
    """List this user's messages. X-Total-Count ignores limit and offset."""
    total = session.scalar(select(func.count(Message.id)).where(Message.user_id == user.id))
    response.headers["X-Total-Count"] = str(total or 0)
    rows = session.exec(
        select(Message)
        .where(Message.user_id == user.id)
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
    item = _message_response(row)
    return MessageDetailResponse(**item.model_dump(), body_text=None, draft=None)


@router.post("/messages/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_messages(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SyncResponse:
    """Enqueue sync_mailbox_delta for the same mailbox GET /users/me reports."""
    account = latest_connected_account(session, user.id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mailbox is connected.",
        )
    await get_arq(request).enqueue_job("sync_mailbox_delta", str(account.id))
    return SyncResponse(enqueued=True)
