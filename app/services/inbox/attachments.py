"""Stage a file from the user's computer and bind it to a draft.

Materials stay on form templates. These rows are the other kind of file.
"""

from datetime import timedelta
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.staged_attachment import StagedAttachment
from app.models.user import utcnow

MAX_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
_UNBOUND_TTL = timedelta(hours=24)
_REJECTED_EXTENSIONS = {".exe", ".bat", ".cmd", ".com", ".msi", ".js", ".html", ".htm", ".svg"}
_REJECTED_TYPES = {
    "application/x-msdownload",
    "application/x-dosexec",
    "text/html",
    "image/svg+xml",
}


def safe_file_name(value: str | None) -> str:
    name = Path(value or "").name.strip()
    if not name or name in {".", ".."} or any(char in name for char in "\r\n\x00"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The file needs a name.",
        )
    return name[:255]


def reject_type(file_name: str, content_type: str | None) -> None:
    extension = Path(file_name).suffix.lower()
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if extension in _REJECTED_EXTENSIONS or media in _REJECTED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="This file type can't be attached.",
        )


def expire_unbound(session: Session, user_id: UUID) -> None:
    """Drop staged files that were never saved onto a draft."""
    import app.storage as storage_api

    cutoff = utcnow() - _UNBOUND_TTL
    rows = session.exec(
        select(StagedAttachment).where(
            StagedAttachment.user_id == user_id,
            StagedAttachment.message_id.is_(None),
            StagedAttachment.created_at < cutoff,
        )
    ).all()
    storage = storage_api.get_storage()
    for row in rows:
        try:
            storage.delete(row.storage_key)
        except Exception:
            pass
        session.delete(row)
    if rows:
        session.commit()


def payloads(session: Session, message_id: UUID) -> list[dict]:
    rows = session.exec(
        select(StagedAttachment)
        .where(StagedAttachment.message_id == message_id)
        .order_by(StagedAttachment.created_at, StagedAttachment.id)
    ).all()
    return [_payload(row) for row in rows]


def _payload(row: StagedAttachment) -> dict:
    return {
        "id": row.id,
        "file_name": row.file_name,
        "size_bytes": row.size_bytes,
        "content_type": row.content_type,
    }


def bind_attachments(session: Session, user_id: UUID, message_id: UUID, attachment_ids: list[UUID]) -> None:
    """Replace the files bound to this message. An unknown id is 404.

    Call `expire_unbound` before adding other rows: it commits. This function
    does not. It does not send the files anywhere.
    """
    if len(attachment_ids) > MAX_FILES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="A draft can include at most 10 files.",
        )
    if len(set(attachment_ids)) != len(attachment_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The same file is listed twice.",
        )
    chosen: list[StagedAttachment] = []
    for attachment_id in attachment_ids:
        row = session.get(StagedAttachment, attachment_id)
        if row is None or row.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
        if row.message_id is None and row.created_at < utcnow() - _UNBOUND_TTL:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
        if row.message_id is not None and row.message_id != message_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
        chosen.append(row)
    total = sum(row.size_bytes for row in chosen)
    if total > MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Attachments must be 20 MB or smaller combined.",
        )
    current = session.exec(
        select(StagedAttachment).where(StagedAttachment.message_id == message_id)
    ).all()
    keep = set(attachment_ids)
    for row in current:
        if row.id not in keep:
            row.message_id = None
            session.add(row)
    for row in chosen:
        row.message_id = message_id
        session.add(row)
