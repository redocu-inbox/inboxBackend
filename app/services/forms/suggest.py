"""The fill job a message list or detail should show."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.dialects.postgresql import distinct_on
from sqlmodel import Session, select

from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate

_SUGGESTED = (FormFillStatus.needs_review.value, FormFillStatus.completed.value)


@dataclass(frozen=True)
class SuggestedForm:
    job_id: UUID
    template_id: UUID
    template_filename: str
    status: str
    confidence: float | None


def suggested_forms_for_messages(
    session: Session, message_ids: list[UUID]
) -> dict[UUID, SuggestedForm]:
    """Newest needs_review or completed job for each message, or nothing.

    Rejected, failed, pending, and running jobs are left out, so a newer
    rejected job does not hide an older completed one. A job with no template
    is left out because the contract requires a template id and filename.
    """
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return {}
    rows = session.exec(
        select(FormFillJob, FormTemplate.filename)
        .join(FormTemplate, FormTemplate.id == FormFillJob.template_id)
        .where(
            FormFillJob.message_id.in_(ids),
            FormFillJob.status.in_(_SUGGESTED),
        )
        .ext(distinct_on(FormFillJob.message_id))
        .order_by(
            FormFillJob.message_id,
            FormFillJob.created_at.desc(),
            FormFillJob.id.desc(),
        )
    ).all()
    found: dict[UUID, SuggestedForm] = {}
    for job, filename in rows:
        if job.message_id is None or job.template_id is None:
            continue
        status = job.status.value if isinstance(job.status, FormFillStatus) else str(job.status)
        found[job.message_id] = SuggestedForm(
            job_id=job.id,
            template_id=job.template_id,
            template_filename=filename,
            status=status,
            confidence=job.confidence,
        )
    return found
