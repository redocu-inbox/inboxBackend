"""Create a local email draft. Nothing here sends mail."""

from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.user import User, utcnow
from app.services.inbox.attachments import bind_attachments, expire_unbound
from app.services.inbox.recipients import MAX_RECIPIENTS, normalize_list
from app.services.llm.factory import get_llm

_MAX_SUBJECT = 300
_MAX_BODY = 12_000


def _clean_text(value: str | None, *, what: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{what} is required.")
        return None
    text = value.strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"{what} is required.")
    limit = _MAX_SUBJECT if what == "Subject" else _MAX_BODY
    if len(text) > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{what} is too long.",
        )
    return text


def _recipients(to: list[str], cc: list[str]) -> tuple[list[str], list[str]]:
    try:
        cleaned_to = normalize_list(to)
        cleaned_cc = normalize_list(cc)
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
    from app.services.inbox.recipients import email_of

    seen = {email_of(item) for item in cleaned_to}
    cc_only = [item for item in cleaned_cc if email_of(item) not in seen]
    if len(cleaned_to) + len(cc_only) > MAX_RECIPIENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="To and Cc together can include at most 50 people.",
        )
    return cleaned_to, cc_only


def _account(session: Session, user: User, account_id: UUID | None) -> ConnectedAccount:
    rows = session.exec(
        select(ConnectedAccount).where(
            ConnectedAccount.user_id == user.id,
            ConnectedAccount.disconnected_at.is_(None),
        )
    ).all()
    if account_id is None:
        if len(rows) == 1:
            return rows[0]
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No mailbox is connected.")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose which mailbox sends this email.",
        )
    for row in rows:
        if row.id == account_id:
            return row
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found.")


def _source(session: Session, user: User, forward_of_id: UUID | None) -> None:
    if forward_of_id is None:
        return
    source = session.get(Message, forward_of_id)
    if source is None or source.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    account = session.get(ConnectedAccount, source.connected_account_id)
    if account is None or account.user_id != user.id or account.disconnected_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email can no longer be forwarded.",
        )


def _materials(session: Session, user: User, material_ids: list[UUID]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[UUID] = set()
    for material_id in material_ids:
        if material_id in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The same material is listed twice.",
            )
        seen.add(material_id)
        row = session.get(FormTemplate, material_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="That file isn't in your materials.",
            )
        found.append({"id": str(row.id), "name": row.filename})
    return found


def _split_sender(recipient: str) -> tuple[str | None, str]:
    if recipient.endswith(">") and " <" in recipient:
        name, email = recipient[:-1].rsplit(" <", 1)
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        return name or None, email
    return None, recipient


async def create_local_email(
    session: Session,
    user: User,
    *,
    account_id: UUID | None,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str | None,
    instructions: str | None,
    material_ids: list[UUID],
    attachment_ids: list[UUID],
    forward_of_id: UUID | None,
) -> tuple[Message, ConnectedAccount]:
    """Save a new email or a forward as a draft. The provider is not called."""
    if forward_of_id is not None and instructions is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A forward is the text you write, not an AI note.",
        )
    if (body is None) == (instructions is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Send the message or a note for the AI, not both.",
        )
    cleaned_subject = _clean_text(subject, what="Subject", required=True)
    assert cleaned_subject is not None
    cleaned_to, cleaned_cc = _recipients(to, cc)
    account = _account(session, user, account_id)
    _source(session, user, forward_of_id)
    materials = _materials(session, user, material_ids)
    if body is not None:
        text = _clean_text(body, what="Message", required=True)
        source = "user"
    else:
        note = _clean_text(instructions, what="Note", required=True)
        try:
            text = await get_llm().complete(
                system="Write a plain-text email from the note. Do not invent facts that are not in the note.",
                user=f"To: {', '.join(cleaned_to)}\nSubject: {cleaned_subject}\n\n{note}",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The draft could not be written.",
            ) from exc
        text = _clean_text(text, what="Message", required=True)
        source = "llm"
    assert text is not None
    expire_unbound(session, user.id)
    from_name, from_email = _split_sender(cleaned_to[0])
    now = utcnow()
    message = Message(
        user_id=user.id,
        connected_account_id=account.id,
        graph_id=f"local-{uuid4()}",
        subject=cleaned_subject,
        body_preview=text[:200],
        from_name=from_name,
        from_email=from_email,
        body_text="",
        received_at=now,
        to_recipients=cleaned_to,
        cc_recipients=cleaned_cc,
        direction="outgoing",
        origin="local",
        forward_of_id=forward_of_id,
        material_attachments=materials,
        created_at=now,
        updated_at=now,
    )
    session.add(message)
    session.flush()
    draft = ReplyDraft(
        user_id=user.id,
        message_id=message.id,
        status=ReplyDraftStatus.ready,
        body=text,
        source=source,
        to_recipients=cleaned_to,
        cc_recipients=cleaned_cc,
        created_at=now,
        updated_at=now,
    )
    session.add(draft)
    if attachment_ids:
        bind_attachments(session, user.id, message.id, attachment_ids)
    session.commit()
    session.refresh(message)
    return message, account
