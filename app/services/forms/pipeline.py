"""End-to-end flow from message content to a completed form."""

from uuid import UUID

from sqlmodel import Session

from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.storage import get_storage


def save_template(
    session: Session,
    *,
    user_id: UUID,
    filename: str,
    data: bytes,
    mime_type: str,
) -> FormTemplate:
    """Store the upload and insert a FormTemplate.

    TODO: later, read the PDF fields into schema_json.
    """
    template = FormTemplate(
        user_id=user_id,
        filename=filename,
        storage_path="",
        mime_type=mime_type,
        tags=[],
        form_schema={},
    )
    key = f"forms/{user_id}/{template.id}/{filename}"
    template.storage_path = get_storage().put(key, data, mime_type)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def create_fill_job(
    session: Session,
    *,
    user_id: UUID,
    template_id: UUID,
    message_id: UUID | None,
) -> FormFillJob:
    """Insert a pending fill job for a template owned by this user."""
    template = session.get(FormTemplate, template_id)
    if template is None or template.user_id != user_id:
        raise LookupError(f"Form template {template_id} was not found")
    job = FormFillJob(
        user_id=user_id,
        template_id=template_id,
        message_id=message_id,
        status=FormFillStatus.pending,
        extracted_values={},
    )
    session.add(job)
    return job