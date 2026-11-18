"""Send one ready draft and record the approval.

Save, sync, and history do not call this. A provider failure rolls the
database back so the draft stays unsent and no approval row is written.
"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, select

from app.models.approval import Approval
from app.models.connected_account import ConnectedAccount
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.staged_attachment import StagedAttachment
from app.models.user import User, utcnow
from app.services.google.oauth import GoogleTokenUnavailable
from app.services.google.tokens import refresh_google_access_token
from app.services.graph.tokens import MailboxNotWritable, TokenRefreshError, needs_refresh, refresh_access_token
from app.services.inbox.recipients import saved_or_default
from app.services.mail import send as mail_send

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
    body = draft.body.strip() if isinstance(draft.body, str) else ""
    if status_value != ReplyDraftStatus.ready.value or not body:
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
        granted = scope_granted(account.scopes, "https://graph.microsoft.com/Mail.Send")
    elif account.provider == "google":
        granted = scope_granted(account.scopes, "https://www.googleapis.com/auth/gmail.send")
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


def _deliver(
    account: ConnectedAccount,
    message: Message,
    *,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
    token: str,
) -> None:
    subject = message.subject or ""
    try:
        if account.provider == "google":
            thread_id = None if message.origin == "local" else message.conversation_id
            mail_send.send_gmail(
                token,
                sender=account.email,
                subject=subject,
                body=body,
                to=to,
                cc=cc,
                files=files,
                thread_id=thread_id,
            )
            return
        if message.origin == "local":
            mail_send.send_outlook_new(
                token,
                subject=subject,
                body=body,
                to=to,
                cc=cc,
                files=files,
            )
            return
        mail_send.send_outlook_reply(
            token,
            message.graph_id,
            body=body,
            to=to,
            cc=cc,
            files=files,
        )
    except mail_send.MailSendError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The mailbox could not send this email.",
        ) from exc


def approve_message(session: Session, user: User, message_id: UUID) -> Message:
    """Send the ready draft once. A second call does not contact the mailbox."""
    message = session.exec(
        select(Message)
        .where(Message.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if message is None or message.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    account = session.get(ConnectedAccount, message.connected_account_id)
    if account is None or account.user_id != user.id or account.disconnected_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    if account.provider == "slack":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use the Slack reply endpoint.",
        )
    if message.approved_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This email was already sent.")
    draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == message.id)).first()
    body = _ready_body(draft)
    assert draft is not None
    to, cc = _recipients(message, draft)
    _require_send_scope(account)
    files = _files(session, message)
    form = _form_snapshot(session, message)
    token = _access_token(session, account)
    account = session.get(ConnectedAccount, account.id) or account
    _require_send_scope(account)
    # Refresh commits and drops the first row lock. Take it again so a second
    # approve waits until this send is recorded, then stops.
    message = session.exec(
        select(Message)
        .where(Message.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if message is None or message.approved_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This email was already sent.")
    _deliver(account, message, body=body, to=to, cc=cc, files=files, token=token)
    sent_at = utcnow()
    message.approved_at = sent_at
    message.updated_at = sent_at
    session.add(message)
    sent_to = ", ".join([*to, *cc])
    session.add(
        Approval(
            user_id=user.id,
            message_id=message.id,
            approved_at=sent_at,
            approved_by_name=user.display_name,
            approved_by_email=user.email,
            sent_from=account.email,
            sent_to=sent_to,
            subject=message.subject,
            reply=body,
            form=form,
            created_at=sent_at,
        )
    )
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This email was already sent.") from exc
    session.refresh(message)
    return message
