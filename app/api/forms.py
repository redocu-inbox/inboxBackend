"""HTTP routes for form submission and status."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_arq, get_current_user, get_session
from app.models.form_fill_job import FormFillJob
from app.models.user import User
from app.services.forms.pipeline import create_fill_job, save_template


router = APIRouter()


class TemplateUploadResponse(BaseModel):
    id: UUID
    filename: str
    mime_type: str


class CreateFillJobRequest(BaseModel):
    template_id: UUID
    message_id: UUID | None = None


class CreateFillJobResponse(BaseModel):
    id: UUID
    status: str


class FillJobResponse(BaseModel):
    status: str
    confidence: float | None
    error: str | None
    extracted_values: dict


@router.post("/forms/templates")
async def upload_template(
    file: UploadFile = File(),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> TemplateUploadResponse:
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only application/pdf is accepted",
        )
    template = save_template(
        session,
        user_id=user.id,
        filename=file.filename or "template.pdf",
        data=await file.read(),
        mime_type="application/pdf",
    )
    return TemplateUploadResponse(
        id=template.id,
        filename=template.filename,
        mime_type=template.mime_type,
    )


@router.post("/forms/jobs")
async def create_job(
    body: CreateFillJobRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> CreateFillJobResponse:
    try:
        job = create_fill_job(
            session,
            user_id=user.id,
            template_id=body.template_id,
            message_id=body.message_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    session.commit()
    session.refresh(job)
    await get_arq(request).enqueue_job("match_form_template", str(job.id))
    return CreateFillJobResponse(id=job.id, status=job.status)


@router.get("/forms/jobs/{job_id}")
def get_job(
    job_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FillJobResponse:
    job = session.get(FormFillJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fill job not found")
    return FillJobResponse(
        status=job.status,
        confidence=job.confidence,
        error=job.error,
        extracted_values=job.extracted_values,
    )
