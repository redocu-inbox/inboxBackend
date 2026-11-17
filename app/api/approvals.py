"""The approval record written by POST /messages/{id}/approve."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_session
from app.models.approval import Approval
from app.models.user import User

router = APIRouter(tags=["approvals"])


class ApprovedBy(BaseModel):
    name: str | None = None
    email: str


class ApprovalFormField(BaseModel):
    label: str
    value: str


class ApprovalForm(BaseModel):
    name: str
    fields: list[ApprovalFormField]


class ApprovalResponse(BaseModel):
    id: UUID
    message_id: UUID | None = None
    approved_at: datetime
    approved_by: ApprovedBy
    sent_from: str | None = None
    sent_to: str
    subject: str | None = None
    reply: str
    form: ApprovalForm | None = None


def _form(value: object) -> ApprovalForm | None:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return None
    raw_fields = value.get("fields")
    fields: list[ApprovalFormField] = []
    if isinstance(raw_fields, list):
        for field in raw_fields:
            if not isinstance(field, dict):
                continue
            label = field.get("label")
            stored = field.get("value")
            if isinstance(label, str):
                fields.append(ApprovalFormField(label=label, value="" if stored is None else str(stored)))
    return ApprovalForm(name=value["name"], fields=fields)


@router.get("/approvals")
def list_approvals(
    response: Response,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[ApprovalResponse]:
    """Newest first. X-Total-Count is this user's full approval count."""
    visible = Approval.user_id == user.id
    total = session.scalar(select(func.count(Approval.id)).where(visible))
    response.headers["X-Total-Count"] = str(total or 0)
    rows = session.exec(
        select(Approval).where(visible).order_by(Approval.approved_at.desc(), Approval.id.desc()).offset(offset).limit(limit)
    ).all()
    return [
        ApprovalResponse(
            id=row.id,
            message_id=row.message_id,
            approved_at=row.approved_at,
            approved_by=ApprovedBy(name=row.approved_by_name, email=row.approved_by_email),
            sent_from=row.sent_from,
            sent_to=row.sent_to,
            subject=row.subject,
            reply=row.reply,
            form=_form(row.form),
        )
        for row in rows
    ]
