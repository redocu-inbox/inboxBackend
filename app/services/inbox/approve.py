"""Submit a saved draft once and retain its snapshot through confirmation."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.approval import Approval
from app.models.connected_account import ConnectedAccount
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.staged_attachment import StagedAttachment
from app.models.user import User, utcnow
from app.services.google.oauth import GMAIL_MODIFY_SCOPE, GMAIL_SEND_SCOPE, GoogleTokenUnavailable
from app.services.google.tokens import refresh_google_access_token
from app.services.graph.tokens import MailboxNotWritable, TokenRefreshError, needs_refresh, refresh_access_token
from app.services.inbox.recipients import saved_or_default
from app.services.mail import send as mail_send
from app.services.mailboxes import lock_active_mailbox

_RECONNECT = "Reconnect this mailbox to send mail."


def scope_granted(stored: str | None, required: str) -> bool:
    """True when the stored grant includes this scope. Mail.ReadWrite is not Mail.Send."""
    if not stored:
        return False
    tail = required.rsplit("/", 1)[-1]
    for part in stored.replace(",", " ").split():
        if part == required or part == tail or part.endswith("/" + tail):
            return True
    return False


def _ready_body(draft: ReplyDraft | None) -> str:
    if draft is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft is not ready.")
    status_value = draft.status.value if isinstance(draft.status, ReplyDraftStatus) else str(draft.status)
    body = draft.body or ""
    if status_value != ReplyDraftStatus.ready.value or not body.strip():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Draft is not ready.")
    return body


def _recipients(message: Message, draft: ReplyDraft) -> tuple[list[str], list[str]]:
    saved = saved_or_default(message, draft.to_recipients, draft.cc_recipients)
    if saved is None or not saved["to"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Add at least one To address.",
        )
    return saved["to"], saved["cc"]


def _load_bytes(key: str) -> bytes | None:
    from app.storage import get_storage

    try:
        return get_storage().get(key)
    except FileNotFoundError:
        return None


def _files(session: Session, message: Message) -> list[tuple[str, bytes, str | None]]:
    """Staged computer files, materials on a new email, and a completed filled PDF.

    A forward does not read files stored on the source message.
    """
    files: list[tuple[str, bytes, str | None]] = []
    staged = session.exec(
        select(StagedAttachment)
        .where(StagedAttachment.message_id == message.id)
        .order_by(StagedAttachment.created_at, StagedAttachment.id)
    ).all()
    for row in staged:
        content = _load_bytes(row.storage_key)
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An attached file is no longer available.",
            )
        files.append((row.file_name, content, row.content_type))
    if message.origin == "local" and isinstance(message.material_attachments, list):
        for item in message.material_attachments:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            try:
                template_id = UUID(item["id"])
            except ValueError:
                continue
            template = session.get(FormTemplate, template_id)
            if template is None or template.user_id != message.user_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An attached file is no longer available.",
                )
            content = _load_bytes(template.storage_path)
            if content is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An attached file is no longer available.",
                )
            files.append((template.filename, content, template.mime_type))
    job = session.exec(
        select(FormFillJob)
        .where(
            FormFillJob.message_id == message.id,
            FormFillJob.user_id == message.user_id,
            FormFillJob.status == FormFillStatus.completed.value,
        )
        .order_by(FormFillJob.created_at.desc())
    ).first()
    if job is not None and job.filled_file_path:
        content = _load_bytes(job.filled_file_path)
        if content is not None:
            name = "filled.pdf"
            if job.template_id is not None:
                template = session.get(FormTemplate, job.template_id)
                if template is not None and template.filename:
                    name = template.filename
            files.append((name, content, "application/pdf"))
    return files


def _form_snapshot(session: Session, message: Message) -> dict | None:
    job = session.exec(
        select(FormFillJob)
        .where(
            FormFillJob.message_id == message.id,
            FormFillJob.user_id == message.user_id,
            FormFillJob.status == FormFillStatus.completed.value,
        )
        .order_by(FormFillJob.created_at.desc())
    ).first()
    if job is None:
        return None
    template = session.get(FormTemplate, job.template_id) if job.template_id else None
    name = template.filename if template is not None and template.filename else "Form"
    values = job.extracted_values if isinstance(job.extracted_values, dict) else {}
    schema_fields = []
    if template is not None and isinstance(template.form_schema, dict):
        raw_fields = template.form_schema.get("fields")
        if isinstance(raw_fields, list):
            schema_fields = raw_fields
    fields: list[dict[str, str]] = []
    if schema_fields:
        for field in schema_fields:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            label = field.get("label") if isinstance(field.get("label"), str) else field["name"]
            value = values.get(field["name"])
            fields.append({"label": label, "value": "" if value is None else str(value)})
    else:
        for key, value in values.items():
            fields.append({"label": str(key), "value": "" if value is None else str(value)})
    return {"name": name, "fields": fields}


def _require_send_scope(account: ConnectedAccount) -> None:
    if account.provider == "microsoft":
        granted = scope_granted(account.scopes, "Mail.Send") and scope_granted(
            account.scopes, "Mail.ReadWrite"
        )
    elif account.provider == "google":
        granted = scope_granted(account.scopes, GMAIL_SEND_SCOPE) or scope_granted(
            account.scopes, GMAIL_MODIFY_SCOPE
        )
    else:
        granted = False
    if not granted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_RECONNECT)


def _access_token(session: Session, account: ConnectedAccount) -> str:
    try:
        if needs_refresh(account):
            if account.provider == "google":
                account = refresh_google_access_token(session, account)
            else:
                account = refresh_access_token(session, account)
    except (TokenRefreshError, MailboxNotWritable, GoogleTokenUnavailable) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The mailbox token could not be refreshed.",
        ) from exc
    if not account.access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The mailbox token could not be refreshed.",
        )
    return account.access_token


def _message(session: Session, user_id: UUID, message_id: UUID, *, lock: bool = False) -> Message:
    message = session.get(Message, message_id, populate_existing=True, with_for_update=lock)
    if message is None or message.user_id != user_id:
        raise HTTPException(404, "Message not found.")
    return message


def pending_send_attempt_id(session: Session, message_id: UUID) -> UUID | None:
    return session.exec(
        select(Approval.id).where(
            Approval.message_id == message_id,
            Approval.approved_at.is_(None),
            Approval.send_failed_at.is_(None),
        )
    ).first()


def lock_outlook_draft_for_edit(
    session: Session, account: ConnectedAccount, message_id: UUID
) -> ConnectedAccount:
    """Serialize provider draft edits with approval's send claim."""
    locked = lock_active_mailbox(session, account.user_id, account_id=account.id)
    if locked is None:
        raise HTTPException(404, "Message not found.")
    message = _message(session, locked.user_id, message_id)
    if message.approved_at is not None or pending_send_attempt_id(session, message_id) is not None:
        raise HTTPException(409, "This Outlook draft has a send attempt.")
    return locked


def _finish_attempt(session: Session, message_id: UUID, attempt_id: UUID, *, sent: bool) -> None:
    message = session.get(Message, message_id, populate_existing=True, with_for_update=True)
    attempt = session.get(Approval, attempt_id, populate_existing=True, with_for_update=True)
    if attempt is None or attempt.approved_at is not None or attempt.send_failed_at is not None:
        session.rollback()
        return
    now = utcnow()
    if sent:
        attempt.approved_at = now
        if message is not None:
            message.approved_at = now
            message.updated_at = now
            session.add(message)
    else:
        attempt.send_failed_at = now
    session.add(attempt)
    session.commit()


def confirm_send_attempt(session: Session, user_id: UUID, message_id: UUID, attempt_id: UUID) -> Approval:
    """Check the owned attempt. Provider confirmation is read-only."""
    message = _message(session, user_id, message_id)
    attempt = session.get(Approval, attempt_id, populate_existing=True)
    if attempt is None or attempt.user_id != user_id or attempt.message_id != message_id:
        raise HTTPException(404, "Send attempt not found.")
    if attempt.approved_at is not None or attempt.send_failed_at is not None:
        return attempt
    reference = attempt.provider_reference
    account = session.get(ConnectedAccount, message.connected_account_id)
    if reference and account is not None and account.user_id == user_id and account.disconnected_at is None:
        try:
            token = _access_token(session, account)
            check = mail_send.gmail_sent if account.provider == "google" else mail_send.outlook_sent
            confirmed = check(token, reference)
        except (HTTPException, mail_send.MailSendError):
            confirmed = False
        if confirmed:
            _finish_attempt(session, message_id, attempt_id, sent=True)
    session.refresh(attempt)
    return attempt


def approve_message(session: Session, user: User, message_id: UUID) -> tuple[Message, UUID | None]:
    """Return message detail and a pending attempt ID when delivery is unconfirmed."""
    user_id = user.id
    message = _message(session, user_id, message_id)
    account_id = message.connected_account_id
    account = lock_active_mailbox(session, user_id, account_id=account_id)
    if account is None:
        raise HTTPException(404, "Message not found.")
    if account.provider == "slack":
        raise HTTPException(409, "Use the Slack reply endpoint.")

    attempt_id = pending_send_attempt_id(session, message_id)
    if attempt_id is None:
        if message.approved_at is not None:
            raise HTTPException(409, "This email was already sent.")
        _require_send_scope(account)
        if needs_refresh(account):
            _access_token(session, account)
            # Token refresh commits. Recheck the mailbox and claim under fresh locks.
            account = lock_active_mailbox(session, user_id, account_id=account_id)
            if account is None:
                raise HTTPException(404, "Message not found.")
        message = _message(session, user_id, message_id, lock=True)
        attempt_id = pending_send_attempt_id(session, message_id)
    if attempt_id is not None:
        session.rollback()
        attempt = confirm_send_attempt(session, user_id, message_id, attempt_id)
        if attempt.send_failed_at is not None:
            raise HTTPException(502, "The mailbox could not send this email.")
        return _message(session, user_id, message_id), None if attempt.approved_at else attempt_id
    if message.approved_at is not None:
        raise HTTPException(409, "This email was already sent.")
    _require_send_scope(account)
    token = account.access_token
    if not token:
        raise HTTPException(502, "The mailbox token could not be refreshed.")

    draft = session.exec(
        select(ReplyDraft).where(ReplyDraft.message_id == message_id).with_for_update()
    ).first()
    body = _ready_body(draft)
    assert draft is not None
    to, cc = _recipients(message, draft)
    files = _files(session, message)
    attempt = Approval(
        user_id=user_id,
        message_id=message_id,
        approved_by_name=user.display_name,
        approved_by_email=user.email,
        sent_from=account.email,
        sent_to=", ".join([*to, *cc]),
        subject=message.subject,
        reply=body,
        form=_form_snapshot(session, message),
    )
    is_gmail = account.provider == "google"
    sender = account.email
    subject = message.subject or ""
    original_id = message.graph_id if message.origin != "local" else None
    thread_id = message.conversation_id if original_id else None
    session.add(attempt)
    session.flush()
    attempt_id = attempt.id
    gmail_payload = None
    try:
        if is_gmail:
            reference = f"{attempt_id}@redocu.app"
            gmail_payload = mail_send.prepare_gmail_message(
                sender=sender, subject=subject, body=body, to=to, cc=cc,
                files=files, thread_id=thread_id, message_id=reference,
            )
        else:
            reference = mail_send.prepare_outlook_draft(
                token, original_id=original_id, saved_draft_id=draft.graph_draft_id,
                subject=subject, body=body, to=to, cc=cc, files=files,
            )
    except mail_send.MailSendError:
        attempt.send_failed_at = utcnow()
        session.commit()
        raise HTTPException(502, "The mailbox could not prepare this email.") from None

    attempt.provider_reference = reference
    # The attempt and its lookup key become visible before the one send request.
    session.commit()
    try:
        if gmail_payload is not None:
            mail_send.send_gmail(token, gmail_payload)
        else:
            mail_send.send_outlook_draft(token, reference)
    except mail_send.MailSendUncertain:
        message = _message(session, user_id, message_id)
        return message, None if message.approved_at is not None else attempt_id
    except mail_send.MailSendError:
        _finish_attempt(session, message_id, attempt_id, sent=False)
        raise HTTPException(502, "The mailbox could not send this email.") from None

    _finish_attempt(session, message_id, attempt_id, sent=True)
    return _message(session, user_id, message_id), None
