"""Message list, detail, and a manual mailbox sync.

The JSON keys are frozen for the frontend. from_name, from_email, is_read, and
body_text are filled when a sync stores them, and stay null when the provider
omits them. body_text is plain text, never raw HTML. Sync writes
priority_score, priority_band, priority_reasons, and the classification
fields. A correction leaves that field as the user saved it. A null reasons
column is [] in JSON. user_corrected is false until a correction.
priority_scored_at is stored and is not returned. suggested_form is null
until a later phase. draft stays null.
"""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, nulls_last
from sqlmodel import Session, select

from app.api.deps import get_arq, get_current_user, get_session
from app.models.classification_correction import (
    CLASSIFICATION_CATEGORIES,
    CLASSIFICATION_FIELDS,
    MessageClassificationCorrection,
)
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.user import User, utcnow
from app.services.mailboxes import mark_sync_enqueue_failed, request_mailbox_sync
from app.services.priority.apply import message_fields
from app.services.priority.score import score

router = APIRouter(tags=["messages"])


class MessageResponse(BaseModel):
    id: UUID
    subject: str | None
    body_preview: str | None
    from_name: str | None = None
    from_email: str | None = None
    received_at: datetime
    is_read: bool | None = None
    provider: str | None = None
    conversation_id: str | None = None
    priority_score: float | None = None
    priority_band: str | None = None
    priority_reasons: list[Any] = Field(default_factory=list)
    category: str | None = None
    category_source: str | None = None
    category_confidence: float | None = None
    project_name: str | None = None
    project_number: str | None = None
    due_date: str | None = None
    responsible_party: str | None = None
    reference_number: str | None = None
    user_corrected: bool = False
    suggested_form: Any | None = None


class MessageDetailResponse(MessageResponse):
    body_text: str | None = None
    draft: Any | None = None


class SyncResponse(BaseModel):
    enqueued: bool
    sync_status: Literal["idle", "pending", "running", "failed"]


_TEXT_LIMITS = {
    "project_name": 120,
    "project_number": 64,
    "responsible_party": 200,
    "reference_number": 64,
}


class ClassificationPatch(BaseModel):
    """Fields the user may correct. Omitted keys stay as stored. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    project_name: str | None = None
    project_number: str | None = None
    due_date: str | None = None
    responsible_party: str | None = None
    reference_number: str | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid category")
        text = value.strip()
        if not text:
            return None
        if text not in CLASSIFICATION_CATEGORIES:
            raise ValueError("invalid category")
        return text

    @field_validator("project_name", "project_number", "responsible_party", "reference_number", mode="before")
    @classmethod
    def _text(cls, value: object, info) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid text")
        text = value.strip()
        if not text:
            return None
        if len(text) > _TEXT_LIMITS[info.field_name]:
            raise ValueError("too long")
        return text

    @field_validator("due_date", mode="before")
    @classmethod
    def _due_date(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid date")
        text = value.strip()
        if not text:
            return None
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            raise ValueError("invalid date") from None
        if parsed.isoformat() != text:
            raise ValueError("invalid date")
        return text


def _visible_message_filter(user_id: UUID):
    """Messages on this user's active mailboxes. Disconnected mailboxes stay in the database."""
    active_ids = select(ConnectedAccount.id).where(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.disconnected_at.is_(None),
    )
    return Message.user_id == user_id, Message.connected_account_id.in_(active_ids)


def _priority_reasons(value: object) -> list[Any]:
    """Null means not scored. A stored list is returned in the stored order."""
    if isinstance(value, list):
        return value
    return []


def _due_date(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _message_response(row: Message, provider: str | None) -> MessageResponse:
    return MessageResponse(
        id=row.id,
        subject=row.subject,
        body_preview=row.body_preview,
        from_name=row.from_name,
        from_email=row.from_email,
        received_at=row.received_at,
        is_read=row.is_read,
        provider=provider,
        priority_score=row.priority_score,
        priority_band=row.priority_band,
        priority_reasons=_priority_reasons(row.priority_reasons),
        category=row.category,
        category_source=row.category_source,
        category_confidence=row.category_confidence,
        project_name=row.project_name,
        project_number=row.project_number,
        due_date=_due_date(row.due_date),
        responsible_party=row.responsible_party,
        reference_number=row.reference_number,
        user_corrected=row.user_corrected is True,
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
    """List this user's mail from every active mailbox. X-Total-Count ignores limit and offset."""
    visible = _visible_message_filter(user.id)
    total = session.scalar(select(func.count(Message.id)).where(*visible))
    response.headers["X-Total-Count"] = str(total or 0)
    rows = session.exec(
        select(Message, ConnectedAccount.provider)
        .join(ConnectedAccount, ConnectedAccount.id == Message.connected_account_id)
        .where(*visible)
        .order_by(*_order_by(sort))
        .offset(offset)
        .limit(limit)
    ).all()
    return [_message_response(row, provider) for row, provider in rows]


def _visible_message(
    session: Session, user: User, message_id: UUID
) -> tuple[Message, ConnectedAccount]:
    """This user's message on an active mailbox, or 404."""
    row = session.get(Message, message_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    account = session.get(ConnectedAccount, row.connected_account_id)
    if account is None or account.user_id != user.id or account.disconnected_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    return row, account


def _detail_response(row: Message, provider: str | None) -> MessageDetailResponse:
    item = _message_response(row, provider)
    return MessageDetailResponse(**item.model_dump(), body_text=row.body_text, draft=None)


def _stored_text(row: Message, field: str) -> str | None:
    value = getattr(row, field)
    if value is None:
        return None
    if field == "due_date":
        return value.isoformat()
    return value


@router.get("/messages/{message_id}")
def read_message(
    message_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse:
    row, account = _visible_message(session, user, message_id)
    return _detail_response(row, account.provider)


@router.patch("/messages/{message_id}/classification")
def correct_classification(
    message_id: UUID,
    body: ClassificationPatch,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse:
    """Save a user correction and re-score from the corrected category and due date.

    Unchanged fields are not logged. A scorer failure rolls the correction back.
    """
    if not body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one field is required.",
        )
    row, account = _visible_message(session, user, message_id)
    changes: list[tuple[str, str | None, str | None]] = []
    for field in CLASSIFICATION_FIELDS:
        if field not in body.model_fields_set:
            continue
        new_value = getattr(body, field)
        old_value = _stored_text(row, field)
        if new_value == old_value:
            continue
        changes.append((field, old_value, new_value))
    if not changes:
        return _detail_response(row, account.provider)

    moment = utcnow()
    for field, old_value, new_value in changes:
        if field == "due_date":
            row.due_date = date.fromisoformat(new_value) if new_value else None
        else:
            setattr(row, field, new_value)
        session.add(
            MessageClassificationCorrection(
                message_id=row.id,
                user_id=user.id,
                field=field,
                original_value=old_value,
                corrected_value=new_value,
                created_at=moment,
            )
        )
    if any(field == "category" for field, _, _ in changes):
        row.category_source = "user"
        row.category_confidence = 1.0
    row.user_corrected = True
    try:
        scored, band, reasons, stamp = score(message_fields(row), moment)
    except Exception:
        session.rollback()
        raise
    row.priority_score = scored
    row.priority_band = band
    row.priority_reasons = reasons
    row.priority_scored_at = stamp
    session.add(row)
    session.commit()
    return _detail_response(row, account.provider)


_SYNC_RANK = {"failed": 3, "running": 2, "pending": 1, "idle": 0}


def _combined_sync_status(requested: list) -> Literal["idle", "pending", "running", "failed"]:
    """The busiest mailbox wins, so one running sync is not hidden by an idle one."""
    status = "idle"
    for item in requested:
        if _SYNC_RANK.get(item.sync_status, 0) > _SYNC_RANK[status]:
            status = item.sync_status
    return status


@router.post("/messages/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_messages(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SyncResponse:
    """Enqueue sync_mailbox_delta for every active mailbox.

    Outlook and Gmail are both queued. `enqueued` is true when this call took
    a new generation for at least one of them. A mailbox that is already
    pending or running is left alone. Queuing does not set last_successful_at.
    A queue outage is 503 and that mailbox is failed, not idle.
    """
    requested = request_mailbox_sync(session, user.id)
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mailbox is connected.",
        )
    enqueued = False
    for item in requested:
        if not item.enqueued:
            continue
        try:
            await get_arq(request).enqueue_job(
                "sync_mailbox_delta",
                str(item.account_id),
                item.generation,
            )
        except Exception as exc:
            mark_sync_enqueue_failed(session, item.account_id, item.generation)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Mailbox sync could not be queued.",
            ) from exc
        enqueued = True
    return SyncResponse(enqueued=enqueued, sync_status=_combined_sync_status(requested))
