"""End-to-end flow from message content to a completed form."""

from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session

from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.user import utcnow
from app.services.forms.schema import (
    TemplateEditError,
    apply_field_labels,
    extract_schema,
)
from app.storage import get_storage


def storage_filename(filename: str | None) -> str:
    """One path segment for the storage key.

    The key is forms/{user_id}/{template_id}/{filename}. A browser can send
    a name with directories or `..`, and LocalStorage rejects a key that
    contains a `..` segment. Fall back when the name is empty or is itself
    `..`.
    """
    raw = (filename or "").replace("\\", "/").replace("\x00", "").strip()
    base = raw.rsplit("/", 1)[-1].strip()
    if not base or base in {".", ".."}:
        return "template.pdf"
    return base[:512]


def save_template(
    session: Session,
    *,
    user_id: UUID,
    filename: str,
    data: bytes,
    mime_type: str,
) -> FormTemplate:
    """Store the upload and insert a FormTemplate with its AcroForm schema.

    The PDF is parsed before anything is written. UnreadablePdf leaves no
    row and no stored object.
    """
    schema = extract_schema(data)
    safe_name = storage_filename(filename)
    template = FormTemplate(
        user_id=user_id,
        filename=safe_name,
        storage_path="",
        mime_type=mime_type,
        tags=[],
        form_schema=schema,
    )
    key = f"forms/{user_id}/{template.id}/{safe_name}"
    template.storage_path = get_storage().put(key, data, mime_type)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def update_template(
    session: Session,
    template: FormTemplate,
    *,
    tags: list[str] | None,
    description: str | None,
    field_labels: dict[str, str] | None,
    set_tags: bool,
    set_description: bool,
    set_field_labels: bool,
) -> FormTemplate:
    """Apply a PATCH. Omitted keys stay as stored. Nothing is written if a rule fails."""
    cleaned_tags = _clean_tags(tags) if set_tags else None
    if set_description and description is not None and len(description) > 2000:
        raise TemplateEditError("Description must be 2,000 characters or fewer.")
    if set_field_labels and field_labels is None:
        raise TemplateEditError("Field labels must be an object.")
    new_schema = (
        apply_field_labels(template.form_schema, field_labels)
        if set_field_labels and field_labels is not None
        else None
    )

    if cleaned_tags is not None:
        template.tags = cleaned_tags
        flag_modified(template, "tags")
    if set_description:
        template.description = description
    if new_schema is not None:
        template.form_schema = new_schema
        flag_modified(template, "form_schema")
    template.updated_at = utcnow()
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def delete_template(session: Session, template: FormTemplate) -> None:
    """Remove the stored PDF, then the row. A missing file still deletes the row."""
    try:
        get_storage().delete(template.storage_path)
    except FileNotFoundError:
        pass
    session.delete(template)
    session.commit()


def _clean_tags(tags: list[str] | None) -> list[str]:
    if tags is None:
        raise TemplateEditError("Tags must be a list.")
    if len(tags) > 20:
        raise TemplateEditError("At most 20 tags.")
    cleaned = []
    for tag in tags:
        text = tag.strip()
        if not 1 <= len(text) <= 40:
            raise TemplateEditError("Each tag must be 1 to 40 characters.")
        cleaned.append(text)
    return cleaned


def create_fill_job(
    session: Session,
    *,
    user_id: UUID,
    template_id: UUID,
    message_id: UUID | None,
    values: dict | None = None,
) -> FormFillJob:
    """Insert a pending fill job for a template and message owned by this user.

    `values` is present when the caller already knows the field values. Those
    are stored as extracted_values and confidence is 1.0. The filler does not
    change that confidence later. Omit `values` to leave confidence unset.
    """
    template = session.get(FormTemplate, template_id)
    if template is None or template.user_id != user_id:
        raise LookupError(f"Form template {template_id} was not found")
    if message_id is not None:
        message = session.get(Message, message_id)
        if message is None or message.user_id != user_id:
            raise LookupError(f"Message {message_id} was not found")
    supplied = values is not None
    job = FormFillJob(
        user_id=user_id,
        template_id=template_id,
        message_id=message_id,
        status=FormFillStatus.pending,
        extracted_values=dict(values) if supplied else {},
        confidence=1.0 if supplied else None,
        dropped_fields=[],
    )
    session.add(job)
    return job