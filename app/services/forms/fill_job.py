"""Run one form-fill job from values already stored on the row.

The filler does not choose a template and does not call a model. It also
does not change confidence. A manual job sets that to 1.0 when it is created.
"""

import logging
import time
from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session

from app.db import engine
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.user import utcnow
from app.services.forms.filler import get_filler
from app.services.forms.schema import schema_summary

logger = logging.getLogger(__name__)

NO_FILLABLE_FIELDS = "This template has no fillable fields."
FILL_FAILED = "The form could not be filled."
_MAX_TRIES = 3


def _retries_remain(ctx: dict | None) -> bool:
    """True when ARQ will run this job again. `job_try` starts at 1."""
    raw = ctx.get("job_try") if isinstance(ctx, dict) else None
    try:
        job_try = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        job_try = 1
    return job_try < _MAX_TRIES


def execute_form_fill(job_id: str, ctx: dict | None = None) -> None:
    """Fill `job_id`. A missing or invalid id is logged and ignored.

    An unexpected error stays running while ARQ will retry. The last try sets
    failed and a short error. The response and the log do not include a
    traceback or the field values.
    """
    try:
        parsed = UUID(str(job_id))
    except (TypeError, ValueError, AttributeError):
        logger.warning("run_form_fill skipped invalid id")
        return
    started = time.perf_counter()
    try:
        finished = _execute(parsed)
    except Exception as exc:
        logger.warning(
            "run_form_fill failed fill_job=%s error_type=%s",
            parsed,
            type(exc).__name__,
        )
        if _retries_remain(ctx):
            raise
        _fail(parsed)
        _log_finished(parsed, started)
        return
    if finished:
        _log_finished(parsed, started)


def _execute(job_id: UUID) -> bool:
    with Session(engine) as session:
        job = session.get(FormFillJob, job_id)
        if job is None:
            logger.warning("fill job %s was not found", job_id)
            return False
        job.status = FormFillStatus.running
        job.updated_at = utcnow()
        session.add(job)
        session.commit()

        template = session.get(FormTemplate, job.template_id)
        if template is None:
            _finish(session, job, status=FormFillStatus.failed, error=FILL_FAILED, dropped=[], pdf=None)
            return True
        kind, _count, _schema = schema_summary(template.form_schema)
        values = job.extracted_values if isinstance(job.extracted_values, dict) else {}
        if kind != "acroform":
            _finish(
                session,
                job,
                status=FormFillStatus.failed,
                error=NO_FILLABLE_FIELDS,
                dropped=[],
                pdf=None,
            )
            return True
        if len(values) == 0:
            _finish(
                session,
                job,
                status=FormFillStatus.needs_review,
                error=None,
                dropped=[],
                pdf=None,
            )
            return True
        data = _storage().get(template.storage_path)
        result = get_filler(template.mime_type).fill(data, values)
        status = FormFillStatus.needs_review if result.dropped else FormFillStatus.completed
        _finish(
            session,
            job,
            status=status,
            error=None,
            dropped=result.dropped,
            pdf=result.pdf,
        )
        return True


def _log_finished(job_id: UUID, started: float) -> None:
    """One line for a finished fill. Field values and message text stay out."""
    with Session(engine) as session:
        job = session.get(FormFillJob, job_id)
        if job is None:
            return
        status = job.status.value if isinstance(job.status, FormFillStatus) else str(job.status)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "run_form_fill finished fill_job=%s status=%s confidence=%s elapsed_ms=%s",
            job_id,
            status,
            job.confidence,
            elapsed_ms,
        )


def _finish(
    session: Session,
    job: FormFillJob,
    *,
    status: FormFillStatus,
    error: str | None,
    dropped: list[str],
    pdf: bytes | None,
) -> None:
    if pdf is None:
        _clear_file(job)
    else:
        key = f"forms/{job.user_id}/{job.id}/filled.pdf"
        job.filled_file_path = _storage().put(key, pdf, "application/pdf")
    job.status = status
    job.error = error
    job.dropped_fields = list(dropped)
    flag_modified(job, "dropped_fields")
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def _fail(job_id: UUID) -> None:
    with Session(engine) as session:
        job = session.get(FormFillJob, job_id)
        if job is None:
            return
        _clear_file(job)
        job.status = FormFillStatus.failed
        job.error = FILL_FAILED
        job.updated_at = utcnow()
        session.add(job)
        session.commit()


def _clear_file(job: FormFillJob) -> None:
    path = job.filled_file_path
    if not path:
        job.filled_file_path = None
        return
    try:
        _storage().delete(path)
    except FileNotFoundError:
        pass
    job.filled_file_path = None


def _storage():
    from app.storage import get_storage

    return get_storage()
