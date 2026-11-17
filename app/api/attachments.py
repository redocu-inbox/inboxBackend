"""Stage one file from the user's computer. Saving a draft is what attaches it."""

from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_current_user, get_session
from app.models.staged_attachment import StagedAttachment
from app.models.user import User, utcnow
from app.services.inbox.attachments import (
    MAX_FILE_BYTES,
    expire_unbound,
    reject_type,
    safe_file_name,
)

import app.storage as storage_api

router = APIRouter(tags=["attachments"])


class StagedFileResponse(BaseModel):
    id: str
    file_name: str
    size_bytes: int
    content_type: str | None = None


@router.post("/attachments", status_code=status.HTTP_201_CREATED)
async def stage_attachment(
    file: UploadFile = File(),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StagedFileResponse:
    """Store one file for this user. It is not part of a draft until a save binds it."""
    file_name = safe_file_name(file.filename)
    content_type = file.content_type
    reject_type(file_name, content_type)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Attachment must be 10 MB or smaller.",
        )
    expire_unbound(session, user.id)
    attachment_id = uuid4()
    key = f"attachments/{user.id}/{attachment_id}"
    storage_api.get_storage().put(key, data, content_type)
    row = StagedAttachment(
        id=attachment_id,
        user_id=user.id,
        file_name=file_name,
        size_bytes=len(data),
        content_type=content_type.split(";", 1)[0].strip() if content_type else None,
        storage_key=key,
        created_at=utcnow(),
    )
    session.add(row)
    session.commit()
    return StagedFileResponse(
        id=str(row.id),
        file_name=row.file_name,
        size_bytes=row.size_bytes,
        content_type=row.content_type,
    )
