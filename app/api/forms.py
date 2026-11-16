"""HTTP routes for form templates and fill jobs."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.api.deps import get_arq, get_current_user, get_session
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.user import User, utcnow
from app.services.forms.pipeline import (
    create_fill_job,
    delete_template,
    save_template,
    update_template,
)
from app.services.forms.schema import TemplateEditError, UnreadablePdf, schema_summary


router = APIRouter()

# 10 MiB. A body of exactly this size is accepted. Anything larger is 413.
_MAX_PDF_BYTES = 10 * 1024 * 1024
_TEMPLATE_NOT_FOUND = "Form template not found."


class TemplateUploadResponse(BaseModel):
    id: UUID
    filename: str
    mime_type: str
    kind: str
    field_count: int


class TemplateSummaryResponse(BaseModel):
    id: UUID
    filename: str
    mime_type: str
    tags: list[str]
    description: str | None
    kind: str
    field_count: int
    created_at: datetime


class TemplateDetailResponse(TemplateSummaryResponse):
    model_config = ConfigDict(populate_by_name=True)

    form_schema: dict = Field(alias="schema")


class PatchTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: list[str] | None = None
    description: str | None = None
    field_labels: dict[str, str] | None = None


class CreateFillJobRequest(BaseModel):
    template_id: UUID | None = None
    message_id: UUID | None = None
    values: dict | None = None


class CreateFillJobResponse(BaseModel):
    id: UUID
    status: str


class RefillJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict


class FillJobResponse(BaseModel):
    id: UUID
    template_id: UUID | None
    message_id: UUID | None
    status: str
    confidence: float | None
    extracted_values: dict
    dropped_fields: list[str]
    error: str | None
    file_ready: bool


_JOB_NOT_FOUND = "Fill job not found"
_TEMPLATE_REQUIRED = "A template is required when values are sent."
_MESSAGE_REQUIRED = "A message is required when values are omitted."


def _storage():
    """Look up storage at call time so tests can replace the factory."""
    from app.storage import get_storage

    return get_storage()


def _summary(template: FormTemplate) -> TemplateSummaryResponse:
    kind, count, _schema = schema_summary(template.form_schema)
    return TemplateSummaryResponse(
        id=template.id,
        filename=template.filename,
        mime_type=template.mime_type,
        tags=list(template.tags or []),
        description=template.description,
        kind=kind,
        field_count=count,
        created_at=template.created_at,
    )


def _detail(template: FormTemplate) -> TemplateDetailResponse:
    kind, count, public = schema_summary(template.form_schema)
    return TemplateDetailResponse(
        id=template.id,
        filename=template.filename,
        mime_type=template.mime_type,
        tags=list(template.tags or []),
        description=template.description,
        kind=kind,
        field_count=count,
        created_at=template.created_at,
        form_schema=public,
    )


def _owned_template(session: Session, user_id: UUID, template_id: UUID) -> FormTemplate:
    template = session.get(FormTemplate, template_id)
    if template is None or template.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_TEMPLATE_NOT_FOUND)
    return template


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
    data = await file.read()
    if len(data) > _MAX_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="PDF must be 10 MB or smaller.",
        )
    try:
        template = save_template(
            session,
            user_id=user.id,
            filename=file.filename or "template.pdf",
            data=data,
            mime_type="application/pdf",
        )
    except UnreadablePdf as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This PDF could not be read.",
        ) from exc
    kind, count, _schema = schema_summary(template.form_schema)
    return TemplateUploadResponse(
        id=template.id,
        filename=template.filename,
        mime_type=template.mime_type,
        kind=kind,
        field_count=count,
    )


@router.get("/forms/templates")
def list_templates(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[TemplateSummaryResponse]:
    rows = session.exec(
        select(FormTemplate)
        .where(FormTemplate.user_id == user.id)
        .order_by(FormTemplate.created_at.desc(), FormTemplate.id.desc())
    ).all()
    return [_summary(row) for row in rows]


@router.get("/forms/templates/{template_id}")
def get_template(
    template_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> TemplateDetailResponse:
    return _detail(_owned_template(session, user.id, template_id))


@router.patch("/forms/templates/{template_id}")
def patch_template(
    template_id: UUID,
    body: PatchTemplateRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> TemplateDetailResponse:
    template = _owned_template(session, user.id, template_id)
    provided = body.model_fields_set
    try:
        updated = update_template(
            session,
            template,
            tags=body.tags,
            description=body.description,
            field_labels=body.field_labels,
            set_tags="tags" in provided,
            set_description="description" in provided,
            set_field_labels="field_labels" in provided,
        )
    except TemplateEditError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.detail,
        ) from exc
    return _detail(updated)


@router.delete("/forms/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_template(
    template_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    delete_template(session, _owned_template(session, user.id, template_id))


def _job_status(value: object) -> str:
    if isinstance(value, FormFillStatus):
        return value.value
    return str(value)


def _job_response(job: FormFillJob) -> FillJobResponse:
    dropped = job.dropped_fields if isinstance(job.dropped_fields, list) else []
    values = job.extracted_values if isinstance(job.extracted_values, dict) else {}
    return FillJobResponse(
        id=job.id,
        template_id=job.template_id,
        message_id=job.message_id,
        status=_job_status(job.status),
        confidence=job.confidence,
        extracted_values=values,
        dropped_fields=[str(name) for name in dropped],
        error=job.error,
        file_ready=bool(job.filled_file_path),
    )


def _owned_job(session: Session, user_id: UUID, job_id: UUID) -> FormFillJob:
    job = session.get(FormFillJob, job_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_JOB_NOT_FOUND)
    return job


@router.post("/forms/jobs")
async def create_job(
    body: CreateFillJobRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> CreateFillJobResponse:
    supplied = "values" in body.model_fields_set and body.values is not None
    if supplied and body.template_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_TEMPLATE_REQUIRED)
    if not supplied and body.message_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_MESSAGE_REQUIRED)
    try:
        job = create_fill_job(
            session,
            user_id=user.id,
            template_id=body.template_id,
            message_id=body.message_id,
            values=body.values if supplied else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    session.commit()
    session.refresh(job)
    job_name = "run_form_fill" if supplied else "match_form_template"
    await get_arq(request).enqueue_job(job_name, str(job.id))
    return CreateFillJobResponse(id=job.id, status=_job_status(job.status))


@router.get("/forms/jobs/{job_id}")
def get_job(
    job_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FillJobResponse:
    return _job_response(_owned_job(session, user.id, job_id))


@router.get("/forms/jobs/{job_id}/file")
def get_job_file(
    job_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    job = _owned_job(session, user.id, job_id)
    if not job.filled_file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_JOB_NOT_FOUND)
    try:
        data = _storage().get(job.filled_file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_JOB_NOT_FOUND) from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="filled.pdf"'},
    )


@router.post("/forms/jobs/{job_id}/refill")
async def refill_job(
    job_id: UUID,
    body: RefillJobRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> CreateFillJobResponse:
    job = _owned_job(session, user.id, job_id)
    if job.filled_file_path:
        try:
            _storage().delete(job.filled_file_path)
        except FileNotFoundError:
            pass
    job.extracted_values = dict(body.values)
    flag_modified(job, "extracted_values")
    job.dropped_fields = []
    flag_modified(job, "dropped_fields")
    job.filled_file_path = None
    job.error = None
    job.status = FormFillStatus.pending
    job.updated_at = utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    await get_arq(request).enqueue_job("run_form_fill", str(job.id))
    return CreateFillJobResponse(id=job.id, status=_job_status(job.status))
