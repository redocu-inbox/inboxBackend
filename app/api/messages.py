from fastapi import APIRouter, Depends, Query
from datetime import datetime
from uuid import UUID


from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_session
from app.models.message import Message
from app.models.user import User


router = APIRouter(tags=["messages"])


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subject: str | None
    body_preview: str | None
    received_at: datetime

@router.get("/messages")
def list_messages(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[MessageResponse]:
    rows = session.exec(
        select(Message)
        .where(Message.user_id == user.id)
        .order_by(Message.received_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [MessageResponse.model_validate(row) for row in rows]