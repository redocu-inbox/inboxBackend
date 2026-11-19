"""Message list, detail, reply drafts, and a manual mailbox sync.

The JSON keys are frozen for the frontend. from_name, from_email, is_read, and
body_text are filled when a sync stores them, and stay null when the provider
omits them. Detail carries plain text and a separate sanitized HTML fragment. Sync writes
priority_score, priority_band, priority_reasons, and the classification
fields. A correction leaves that field as the user saved it. A null reasons
column is [] in JSON. user_corrected is false until a correction.
priority_scored_at is stored and is not returned. suggested_form is the
newest needs_review or completed fill job for that message, or null.
detail.draft is the local reply draft, or null. has_draft reports saved reply text.
The list omits archived and trashed messages, draft details, and message bodies.
"""

import logging
from datetime import date, datetime
from typing import Any, Literal
from urllib.error import HTTPError
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import func, nulls_last
from sqlalchemy.orm import defer
from sqlmodel import Session, select

from app.api.deps import get_arq, get_current_user, get_session
from app.models.classification_correction import (
    CLASSIFICATION_CATEGORIES,
    CLASSIFICATION_FIELDS,
    MessageClassificationCorrection,
)
from app.models.connected_account import ConnectedAccount
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.user import User, utcnow
from app.services.forms.suggest import SuggestedForm, suggested_forms_for_messages
from app.services.google.oauth import GMAIL_MODIFY_SCOPE, GoogleTokenUnavailable
from app.services.google.tokens import refresh_google_access_token
from app.services.graph.drafts import (
    GraphDraftError,
    add_file_attachment,
    create_reply_draft,
    patch_reply_draft,
)
from app.services.graph.html_text import MAX_PLAIN_TEXT_CHARS
from app.services.graph.tokens import (
    MailboxNotWritable,
    TokenRefreshError,
    needs_refresh,
    refresh_access_token,
)
from app.services.inbox.approve import (
    approve_message,
    confirm_send_attempt,
    lock_outlook_draft_for_edit,
    pending_send_attempt_id,
    scope_granted,
)
from app.services.inbox.attachments import payloads as attachment_payloads
from app.services.inbox.attachments import bind_attachments, expire_unbound
from app.services.inbox.compose import create_local_email
from app.services.inbox.history import conversation_page
from app.services.inbox.mail_actions import MoveAction, write_provider_message
from app.services.inbox.recipients import normalize_list, reply_all, saved_or_default
from app.services.mailboxes import lock_active_mailbox, mark_sync_enqueue_failed, request_mailbox_sync
from app.services.priority.apply import message_fields
from app.services.priority.score import score

router = APIRouter(tags=["messages"])
logger = logging.getLogger(__name__)


_WRITE_SCOPES = {
    "microsoft": "https://graph.microsoft.com/Mail.ReadWrite",
    "google": GMAIL_MODIFY_SCOPE,
}
_RECONNECT_FOR_ACTIONS = "Reconnect this mailbox to enable email actions."


class DraftRecipientsResponse(BaseModel):
    """Saved outgoing addresses. Absent when a reply has nobody to answer."""

    to: list[str]
    cc: list[str]


class DraftFileResponse(BaseModel):
    """One file staged from the computer and bound to this draft."""

    id: UUID
    file_name: str
    size_bytes: int
    content_type: str | None = None


class ReplyDraftResponse(BaseModel):
    """Frozen draft object. `created_at` stays on the table and is not returned."""

    id: UUID
    message_id: UUID
    status: str
    body: str | None = None
    source: str | None = None
    graph_draft_id: str | None = None
    error: str | None = None
    updated_at: datetime
    recipients: DraftRecipientsResponse | None = None
    attachments: list[DraftFileResponse] = Field(default_factory=list)


class MaterialRefResponse(BaseModel):
    id: str
    name: str


class NewEmailResponse(BaseModel):
    """Set only on an email this app composed. Received mail leaves it null."""

    to: list[str]
    cc: list[str]
    attachments: list[MaterialRefResponse] = Field(default_factory=list)
    forward_of_id: UUID | None = None


class SuggestedFormResponse(BaseModel):
    """Newest fill worth showing. Rejected and in-progress jobs are omitted."""

    job_id: UUID
    template_id: UUID
    template_filename: str
    status: str
    confidence: float | None = None


class MessageResponse(BaseModel):
    id: UUID
    subject: str | None
    body_preview: str | None
    from_name: str | None = None
    from_email: str | None = None
    received_at: datetime
    is_read: bool | None = None
    provider: str | None = None
    has_draft: bool
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
    suggested_form: SuggestedFormResponse | None = None
    new_email: NewEmailResponse | None = None
    approved_at: datetime | None = None
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    slack_permalink: str | None = None


class MessageDetailResponse(MessageResponse):
    body_text: str | None = None
    body_html: str | None = None
    draft: ReplyDraftResponse | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    reply_to: list[str] = Field(default_factory=list)
    reply_all_recipients: DraftRecipientsResponse | None = None
    can_edit_recipients: bool = False
    account_id: UUID | None = None
    account_email: str | None = None
    pending_send_attempt_id: UUID | None = None


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
    """Inbox messages on this user's active mailboxes."""
    active_ids = select(ConnectedAccount.id).where(
        ConnectedAccount.user_id == user_id,
        ConnectedAccount.disconnected_at.is_(None),
    )
    return (
        Message.user_id == user_id,
        Message.connected_account_id.in_(active_ids),
        Message.inbox_removed_at.is_(None),
    )


def _priority_reasons(value: object) -> list[Any]:
    """Null means not scored. A stored list is returned in the stored order."""
    if isinstance(value, list):
        return value
    return []


def _due_date(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _suggested_response(suggested: SuggestedForm | None) -> SuggestedFormResponse | None:
    if suggested is None:
        return None
    return SuggestedFormResponse(
        job_id=suggested.job_id,
        template_id=suggested.template_id,
        template_filename=suggested.template_filename,
        status=suggested.status,
        confidence=suggested.confidence,
    )


def _message_response(
    row: Message, provider: str | None, suggested: SuggestedForm | None, *, draft_text: str | None
) -> MessageResponse:
    return MessageResponse(
        id=row.id,
        subject=row.subject,
        body_preview=row.body_preview,
        from_name=row.from_name,
        from_email=row.from_email,
        received_at=row.received_at,
        is_read=row.is_read,
        provider=provider,
        has_draft=bool(draft_text and draft_text.strip()),
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
        suggested_form=_suggested_response(suggested),
        conversation_id=row.conversation_id,
        new_email=_new_email(row),
        approved_at=row.approved_at,
        slack_channel_id=row.slack_channel_id,
        slack_thread_ts=row.slack_thread_ts,
        slack_permalink=row.slack_permalink,
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
        select(Message, ConnectedAccount.provider, ReplyDraft.body)
        .join(ConnectedAccount, ConnectedAccount.id == Message.connected_account_id)
        .outerjoin(ReplyDraft, ReplyDraft.message_id == Message.id)
        .options(defer(Message.body_html))
        .where(*visible)
        .order_by(*_order_by(sort))
        .offset(offset)
        .limit(limit)
    ).all()
    suggestions = suggested_forms_for_messages(session, [row.id for row, _, _ in rows])
    return [
        _message_response(row, provider, suggestions.get(row.id), draft_text=draft_text)
        for row, provider, draft_text in rows
    ]


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


def _draft_status(value: object) -> str:
    if isinstance(value, ReplyDraftStatus):
        return value.value
    return str(value)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _new_email(row: Message) -> NewEmailResponse | None:
    if row.origin != "local":
        return None
    materials = []
    if isinstance(row.material_attachments, list):
        for item in row.material_attachments:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str):
                materials.append(MaterialRefResponse(id=item["id"], name=item["name"]))
    return NewEmailResponse(
        to=_string_list(row.to_recipients),
        cc=_string_list(row.cc_recipients),
        attachments=materials,
        forward_of_id=row.forward_of_id,
    )


def _draft_response(session: Session, row: ReplyDraft) -> ReplyDraftResponse:
    message = session.get(Message, row.message_id)
    recipients = None
    if message is not None:
        saved = saved_or_default(message, row.to_recipients, row.cc_recipients)
        if saved is not None:
            recipients = DraftRecipientsResponse(to=saved["to"], cc=saved["cc"])
    return ReplyDraftResponse(
        id=row.id,
        message_id=row.message_id,
        status=_draft_status(row.status),
        body=row.body,
        source=row.source,
        graph_draft_id=row.graph_draft_id,
        error=row.error,
        updated_at=row.updated_at,
        recipients=recipients,
        attachments=[DraftFileResponse(**item) for item in attachment_payloads(session, row.message_id)],
    )


def _load_draft(session: Session, message_id: UUID) -> ReplyDraft | None:
    return session.exec(select(ReplyDraft).where(ReplyDraft.message_id == message_id)).first()


def _detail_response(
    session: Session, row: Message, account: ConnectedAccount
) -> MessageDetailResponse:
    suggested = suggested_forms_for_messages(session, [row.id]).get(row.id)
    draft = _load_draft(session, row.id)
    item = _message_response(row, account.provider, suggested, draft_text=draft.body if draft else None)
    draft_body = _draft_response(session, draft) if draft is not None else None
    everyone = reply_all(row, account)
    reply_all_recipients = (
        DraftRecipientsResponse(to=everyone["to"], cc=everyone["cc"]) if everyone["to"] else None
    )
    # True only when a draft already has a To list approve will send as saved.
    can_edit = draft_body is not None and draft_body.recipients is not None
    return MessageDetailResponse(
        **item.model_dump(),
        body_text=row.body_text,
        body_html=row.body_html,
        draft=draft_body,
        to=_string_list(row.to_recipients),
        cc=_string_list(row.cc_recipients),
        reply_to=_string_list(row.reply_to),
        reply_all_recipients=reply_all_recipients,
        can_edit_recipients=can_edit,
        account_id=account.id,
        account_email=account.email,
        pending_send_attempt_id=pending_send_attempt_id(session, row.id) if row.approved_at is None else None,
    )


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
    return _detail_response(session, row, account)


class NewEmailInput(BaseModel):
    """A new email or a forward. Creating it does not send."""

    model_config = ConfigDict(extra="forbid")

    account_id: UUID | None = None
    to: list[str]
    cc: list[str] = Field(default_factory=list)
    subject: str
    body: str | None = None
    instructions: str | None = None
    material_ids: list[UUID] = Field(default_factory=list)
    attachment_ids: list[UUID] = Field(default_factory=list)
    forward_of_id: UUID | None = None


class ConversationMessage(BaseModel):
    id: UUID
    from_name: str | None = None
    from_email: str | None = None
    to: list[str]
    cc: list[str]
    body_text: str
    received_at: datetime
    direction: Literal["incoming", "outgoing"]
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    slack_permalink: str | None = None


class ConversationPage(BaseModel):
    messages: list[ConversationMessage]
    next_cursor: str | None = None


@router.post("/messages/new", status_code=status.HTTP_201_CREATED)
async def create_new_email(
    body: NewEmailInput,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse:
    """Save a new email or a forward. The mailbox is not contacted."""
    message, account = await create_local_email(
        session,
        user,
        account_id=body.account_id,
        to=body.to,
        cc=body.cc,
        subject=body.subject,
        body=body.body,
        instructions=body.instructions,
        material_ids=body.material_ids,
        attachment_ids=body.attachment_ids,
        forward_of_id=body.forward_of_id,
    )
    return _detail_response(session, message, account)


class ApprovalPendingResponse(BaseModel):
    send_status: Literal["confirmation_pending"] = "confirmation_pending"
    attempt_id: UUID


class SendAttemptResponse(BaseModel):
    attempt_id: UUID
    send_status: Literal["confirmation_pending", "sent", "failed"]
    approved_at: datetime | None = None


@router.post(
    "/messages/{message_id}/approve",
    response_model=MessageDetailResponse,
    responses={202: {"model": ApprovalPendingResponse}},
)
def approve_draft(
    message_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> MessageDetailResponse | JSONResponse:
    """Send the ready draft once. This is the only message route that sends mail."""
    message, pending_id = approve_message(session, user, message_id)
    if pending_id is not None:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=ApprovalPendingResponse(attempt_id=pending_id).model_dump(mode="json"),
        )
    account = session.get(ConnectedAccount, message.connected_account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    return _detail_response(session, message, account)


@router.get("/messages/{message_id}/send-attempts/{attempt_id}")
def read_send_attempt(
    message_id: UUID,
    attempt_id: UUID,
    response: Response,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SendAttemptResponse:
    attempt = confirm_send_attempt(session, user.id, message_id, attempt_id)
    response.headers["Cache-Control"] = "no-store"
    send_status: Literal["confirmation_pending", "sent", "failed"] = "confirmation_pending"
    if attempt.approved_at is not None:
        send_status = "sent"
    elif attempt.send_failed_at is not None:
        send_status = "failed"
    return SendAttemptResponse(attempt_id=attempt.id, send_status=send_status, approved_at=attempt.approved_at)


@router.get("/messages/{message_id}/conversation")
def read_conversation(
    message_id: UUID,
    cursor: str | None = None,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ConversationPage:
    """Plain-text history for this mailbox's copy of the thread. Drafts are omitted."""
    row, account = _visible_message(session, user, message_id)
    page = conversation_page(session, row, account, cursor)
    return ConversationPage.model_validate(page)


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
        return _detail_response(session, row, account)

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
    return _detail_response(session, row, account)


class ReplyDraftPatch(BaseModel):
    """Replace the local reply text, and optionally who it goes to or which files it keeps.

    Extra keys are rejected. Omitted recipient and file fields stay as they are.
    `attachmentIds` is the name the app sends; `attachment_ids` is accepted too.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    body: str | None = None
    to: list[str] | None = None
    cc: list[str] | None = None
    attachment_ids: list[UUID] | None = Field(default=None, alias="attachmentIds")

    @field_validator("body", mode="before")
    @classmethod
    def _body(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid body")
        text = value.strip()
        if not text:
            raise ValueError("empty body")
        if len(text) > MAX_PLAIN_TEXT_CHARS:
            raise ValueError("too long")
        return text


class DraftAttachmentRequest(BaseModel):
    """Completed fill job to attach. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    form_fill_job_id: UUID


# 3 MiB. A PDF of exactly this size is accepted. Anything larger is 409.
_MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024
_FORM_NOT_READY = "This form is not ready to attach."
_FORM_OTHER_MESSAGE = "This form belongs to another message."
_FORM_TOO_LARGE = "PDF must be 3 MB or smaller."
_JOB_NOT_FOUND = "Fill job not found"


def _ready_draft(draft: ReplyDraft | None) -> bool:
    if draft is None or _draft_status(draft.status) != ReplyDraftStatus.ready.value:
        return False
    return isinstance(draft.body, str) and bool(draft.body.strip())


def _store_outlook_error(session: Session, draft: ReplyDraft, error: str) -> None:
    """Keep status ready and keep the body. The stored error is a short code."""
    current = session.get(ReplyDraft, draft.id)
    if current is None:
        return
    current.error = error
    current.status = ReplyDraftStatus.ready
    current.updated_at = utcnow()
    session.add(current)
    session.commit()


@router.post("/messages/{message_id}/draft")
async def create_reply_draft_job(
    message_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ReplyDraftResponse:
    """Create or reset the local draft to pending and enqueue generation.

    `error` and `graph_draft_id` are cleared. `body` is kept. This does not
    call Graph.
    """
    row, _account = _visible_message(session, user, message_id)
    draft = _load_draft(session, row.id)
    now = utcnow()
    if draft is None:
        draft = ReplyDraft(
            user_id=row.user_id,
            message_id=row.id,
            status=ReplyDraftStatus.pending,
            created_at=now,
            updated_at=now,
        )
    else:
        draft.status = ReplyDraftStatus.pending
        draft.error = None
        draft.graph_draft_id = None
        draft.updated_at = now
    session.add(draft)
    session.commit()
    session.refresh(draft)
    try:
        await get_arq(request).enqueue_job("generate_reply_draft", str(row.id))
    except Exception as exc:
        draft.status = ReplyDraftStatus.failed
        draft.error = "generation failed"
        draft.updated_at = utcnow()
        session.add(draft)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reply draft could not be queued.",
        ) from exc
    return _draft_response(session, draft)


@router.get("/messages/{message_id}/draft")
def read_reply_draft(
    message_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ReplyDraftResponse:
    _visible_message(session, user, message_id)
    draft = _load_draft(session, message_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    return _draft_response(session, draft)


@router.patch("/messages/{message_id}/draft")
def update_reply_draft(
    message_id: UUID,
    body: ReplyDraftPatch,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ReplyDraftResponse:
    """Save trimmed text, recipients, or staged files. Does not call the mailbox.

    `can_edit_recipients` stays false until a later approve path sends the saved
    addresses. The addresses are still stored and echoed.
    """
    row, _account = _visible_message(session, user, message_id)
    draft = _load_draft(session, message_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    fields = body.model_fields_set
    if not fields or (body.body is None and "to" not in fields and "cc" not in fields and "attachment_ids" not in fields):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Nothing to save.")
    if "attachment_ids" in fields:
        expire_unbound(session, user.id)
    if body.body is not None:
        draft.body = body.body
        draft.source = "user"
        draft.status = ReplyDraftStatus.ready
        draft.error = None
    if "to" in fields or "cc" in fields:
        current_to = draft.to_recipients if isinstance(draft.to_recipients, list) else None
        current_cc = draft.cc_recipients if isinstance(draft.cc_recipients, list) else []
        if current_to is None:
            fallback = saved_or_default(row, None, None)
            current_to = fallback["to"] if fallback else []
            current_cc = []
        next_to = body.to if "to" in fields else current_to
        next_cc = body.cc if "cc" in fields else current_cc
        if next_to is None:
            next_to = []
        if next_cc is None:
            next_cc = []
        try:
            cleaned_to = normalize_list(next_to)
            cleaned_cc = normalize_list(next_cc)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Enter a valid email address.",
            ) from exc
        if not cleaned_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Add at least one To address.",
            )
        from app.services.inbox.recipients import MAX_RECIPIENTS, email_of

        seen = {email_of(item) for item in cleaned_to}
        cleaned_cc = [item for item in cleaned_cc if email_of(item) not in seen]
        if len(cleaned_to) + len(cleaned_cc) > MAX_RECIPIENTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="To and Cc together can include at most 50 people.",
            )
        draft.to_recipients = cleaned_to
        draft.cc_recipients = cleaned_cc
        if row.origin == "local":
            row.to_recipients = cleaned_to
            row.cc_recipients = cleaned_cc
            session.add(row)
    if "attachment_ids" in fields:
        bind_attachments(session, user.id, row.id, body.attachment_ids or [])
    draft.updated_at = utcnow()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return _draft_response(session, draft)


@router.delete("/messages/{message_id}/draft", status_code=status.HTTP_204_NO_CONTENT)
def delete_reply_draft(
    message_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Remove the local row only. Outlook is not changed."""
    _visible_message(session, user, message_id)
    draft = _load_draft(session, message_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    session.delete(draft)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/messages/{message_id}/draft/outlook")
def push_outlook_draft(
    message_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ReplyDraftResponse:
    """Save an unsent Outlook draft. createReply runs only when no Graph id is stored.

    A later push PATCHes that same id. This route does not send mail.
    """
    row, account = _visible_message(session, user, message_id)
    if account.provider == "slack":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use the Slack reply endpoint.",
        )
    if account.provider != "microsoft":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only an Outlook mailbox can save this draft.",
        )
    draft = _load_draft(session, row.id)
    if not _ready_draft(draft) or draft is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft is not ready.")
    provider_message_id = row.graph_id
    token = _outlook_access_token(session, account, draft, row.id)
    draft = session.get(ReplyDraft, draft.id, populate_existing=True, with_for_update=True)
    if not _ready_draft(draft) or draft is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft is not ready.")
    try:
        draft_id = draft.graph_draft_id
        if not draft_id:
            draft_id = create_reply_draft(token, provider_message_id)
            draft.graph_draft_id = draft_id
        patch_reply_draft(token, draft_id, draft.body or "")
    except GraphDraftError:
        _store_outlook_error(session, draft, "outlook draft failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Outlook draft could not be saved.",
        ) from None
    saved = session.get(ReplyDraft, draft.id)
    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    saved.error = None
    saved.status = ReplyDraftStatus.ready
    saved.updated_at = utcnow()
    session.add(saved)
    session.commit()
    session.refresh(saved)
    return _draft_response(session, saved)


def _storage():
    """Look up storage at call time so tests can replace the factory."""
    from app.storage import get_storage

    return get_storage()


def _fill_status(value: object) -> str:
    if isinstance(value, FormFillStatus):
        return value.value
    return str(value)


def _filled_pdf(job: FormFillJob) -> bytes | None:
    if not job.filled_file_path:
        return None
    try:
        return _storage().get(job.filled_file_path)
    except FileNotFoundError:
        return None


def _attachment_name(session: Session, job: FormFillJob) -> str:
    if job.template_id is None:
        return "filled.pdf"
    template = session.get(FormTemplate, job.template_id)
    if template is None or not template.filename:
        return "filled.pdf"
    return template.filename


def _outlook_access_token(
    session: Session, account: ConnectedAccount, draft: ReplyDraft, message_id: UUID
) -> str:
    try:
        if needs_refresh(account):
            account = refresh_access_token(session, account)
    except (TokenRefreshError, MailboxNotWritable) as exc:
        logger.warning(
            "outlook draft refresh failed message=%s error_type=%s",
            message_id,
            type(exc).__name__,
        )
        _store_outlook_error(session, draft, "token refresh failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Outlook token could not be refreshed.",
        ) from exc
    token = lock_outlook_draft_for_edit(session, account, message_id).access_token
    if not token:
        _store_outlook_error(session, draft, "token refresh failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Outlook token could not be refreshed.",
        )
    return token


@router.post("/messages/{message_id}/draft/attachments")
def attach_filled_form(
    message_id: UUID,
    body: DraftAttachmentRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ReplyDraftResponse:
    """Attach a completed PDF to an unsent Outlook draft. This route does not send mail.

    A missing Graph draft id runs the same createReply and body PATCH as the
    Outlook push, then adds the file. An id that is already stored is left
    as it is and only the attachment is posted.
    """
    row, account = _visible_message(session, user, message_id)
    if account.provider == "slack":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use the Slack reply endpoint.",
        )
    if account.provider != "microsoft":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only an Outlook mailbox can save this draft.",
        )
    job = session.get(FormFillJob, body.form_fill_job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_JOB_NOT_FOUND)
    if _fill_status(job.status) != FormFillStatus.completed.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_FORM_NOT_READY)
    if job.message_id != row.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_FORM_OTHER_MESSAGE)
    pdf = _filled_pdf(job)
    if pdf is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_FORM_NOT_READY)
    if len(pdf) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_FORM_TOO_LARGE)
    filename = _attachment_name(session, job)
    draft = _load_draft(session, row.id)
    if not _ready_draft(draft) or draft is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft is not ready.")
    provider_message_id = row.graph_id
    token = _outlook_access_token(session, account, draft, row.id)
    draft = session.get(ReplyDraft, draft.id, populate_existing=True, with_for_update=True)
    if not _ready_draft(draft) or draft is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft is not ready.")
    try:
        draft_id = draft.graph_draft_id
        if not draft_id:
            draft_id = create_reply_draft(token, provider_message_id)
            draft.graph_draft_id = draft_id
            patch_reply_draft(token, draft_id, draft.body or "")
        add_file_attachment(token, draft_id, filename, pdf)
    except GraphDraftError:
        _store_outlook_error(session, draft, "outlook draft failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Outlook draft could not be saved.",
        ) from None
    saved = session.get(ReplyDraft, draft.id)
    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    saved.error = None
    saved.status = ReplyDraftStatus.ready
    saved.updated_at = utcnow()
    session.add(saved)
    session.commit()
    session.refresh(saved)
    return _draft_response(session, saved)


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


class ReadStateInput(BaseModel):
    is_read: StrictBool


def _apply_mail_change(
    session: Session,
    user: User,
    message_id: UUID,
    change: bool | MoveAction,
) -> None:
    row, _ = _visible_message(session, user, message_id)
    user_id = user.id
    account_id = row.connected_account_id

    account = lock_active_mailbox(session, user_id, account_id=account_id)
    if account is None:
        raise HTTPException(404, "Message not found.")

    scope = _WRITE_SCOPES.get(account.provider)
    if row.origin != "provider" or scope is None:
        raise HTTPException(409, "This action is only available for synced email.")
    if not scope_granted(account.scopes, scope):
        raise HTTPException(409, _RECONNECT_FOR_ACTIONS)

    if needs_refresh(account) or not account.access_token:
        refresh = (
            refresh_google_access_token
            if account.provider == "google"
            else refresh_access_token
        )
        try:
            refresh(session, account, generation=account.sync_generation)
        except MailboxNotWritable:
            raise HTTPException(404, "Message not found.") from None
        except TokenRefreshError:
            raise HTTPException(409, _RECONNECT_FOR_ACTIONS) from None
        except GoogleTokenUnavailable:
            raise HTTPException(502, "The mailbox token could not be refreshed.") from None

        # The refresh helper commits, so acquire the lock again.
        account = lock_active_mailbox(session, user_id, account_id=account_id)
        if account is None:
            raise HTTPException(404, "Message not found.")

    row = session.get(Message, message_id, populate_existing=True, with_for_update=True)
    if row is None or row.inbox_removed_at is not None:
        raise HTTPException(404, "Inbox message not found.")

    token = account.access_token
    if not token or not scope_granted(account.scopes, scope):
        raise HTTPException(409, _RECONNECT_FOR_ACTIONS)

    try:
        write_provider_message(account.provider, token, row.graph_id, change)
    except HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise HTTPException(409, _RECONNECT_FOR_ACTIONS) from None
        if exc.code == 404:
            raise HTTPException(404, "Message no longer available.") from None
        raise HTTPException(502, "The provider could not update this message.") from None
    except OSError:
        raise HTTPException(502, "The provider could not update this message.") from None

    now = utcnow()
    if isinstance(change, bool):
        row.is_read = change
    else:
        row.inbox_removed_at = now
    row.updated_at = now

    # A worker may already have fetched the pre-action version of the inbox.
    account.sync_generation = int(account.sync_generation or 0) + 1
    if account.sync_status in {"pending", "running"}:
        account.sync_status = "idle"
        account.sync_status_at = now
        account.sync_error_code = None

    session.add(row)
    session.add(account)
    session.commit()


@router.patch("/messages/{message_id}/read-state", status_code=204)
def set_message_read_state(
    message_id: UUID,
    body: ReadStateInput,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    _apply_mail_change(session, user, message_id, body.is_read)
    return Response(status_code=204)


@router.post("/messages/{message_id}/actions/{action}", status_code=204)
def move_inbox_message(
    message_id: UUID,
    action: MoveAction,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    _apply_mail_change(session, user, message_id, action)
    return Response(status_code=204)
