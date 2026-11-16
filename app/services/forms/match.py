"""Choose a template for one message and ask the model for field values.

Scoring is a token overlap count. It does not use embeddings or pgvector.
The model call happens after the database session closes. The stub, and any
reply that is not a JSON object with at least one field, stops at
needs_review and does not enqueue the filler.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.user import utcnow
from app.services.forms.schema import schema_summary
from app.services.llm.factory import get_llm

logger = logging.getLogger(__name__)

NO_MATCH = "No matching form."
MATCH_FAILED = "The form could not be matched."
_MESSAGE_MISSING = "The message was not found."
_TEMPLATE_MISSING = "The template was not found."
_TOKEN = re.compile(r"[A-Za-z]{4,}")


@dataclass(frozen=True)
class MatchReady:
    template_id: UUID
    confidence: float
    system: str
    user_prompt: str


def tokens(text: str | None) -> set[str]:
    """Letter runs of at least four characters, compared without case."""
    return {match.casefold() for match in _TOKEN.findall(text or "")}


def template_tokens(filename: str, tags: list[str] | None, description: str | None) -> set[str]:
    """Tokens from the filename stem, the tags, and the description."""
    parts = [Path(filename).stem, description or ""]
    parts.extend(tags or [])
    found: set[str] = set()
    for part in parts:
        found |= tokens(part)
    return found


def message_text(subject: str | None, body_text: str | None, body_preview: str | None) -> str:
    """Subject plus body. A missing body uses the preview."""
    body = body_text if body_text else (body_preview or "")
    return f"{subject or ''}\n{body}".strip()


def choose_score(scores: list[int]) -> int | None:
    """Index of the winner. None when the best score is below 2 or tied."""
    if not scores:
        return None
    best = max(scores)
    if best < 2:
        return None
    winners = [index for index, score in enumerate(scores) if score == best]
    if len(winners) != 1:
        return None
    return winners[0]


def build_extract_prompts(fields: list[dict], text: str) -> tuple[str, str]:
    """Ask for one JSON object of strings, keyed by field name."""
    system = (
        "Extract form values from the message. "
        "Reply with one JSON object and no other text. "
        "Use the field names as keys. Every value must be a string. "
        "Omit a field when the message does not say what to put there."
    )
    lines = []
    for field in fields:
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue
        bits = [name, str(field.get("type") or "text")]
        label = field.get("label")
        if isinstance(label, str) and label:
            bits.append(f"label {label}")
        options = field.get("options")
        if isinstance(options, list) and options:
            bits.append("options " + ", ".join(str(item) for item in options))
        lines.append("- " + " | ".join(bits))
    listed = "\n".join(lines) if lines else "- (no fields)"
    return system, f"Fields:\n{listed}\n\nMessage:\n{text}"


def parse_extracted_object(raw: str) -> dict | None:
    """A JSON object with at least one field. Anything else, including the stub, is None."""
    try:
        parsed = json.loads(raw.strip())
    except (AttributeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    if any(not isinstance(key, str) for key in parsed):
        return None
    return parsed


def consider_form_match_on_sync(ctx: dict, account_id: UUID) -> None:
    """A finished sync does not enqueue a form match.

    FORM_MATCH_ON_SYNC defaults to false. Turning it on is a later change.
    This function does not enqueue match_form_template in either case.
    """
    del account_id
    if get_settings().form_match_on_sync:
        logger.info(
            "FORM_MATCH_ON_SYNC is set; sync does not enqueue a match job_id=%s",
            ctx.get("job_id"),
        )


async def execute_form_match(ctx: dict, job_id: str) -> None:
    """Match `job_id` and enqueue a fill when the model returns field values.

    A missing id is logged and ignored. An unexpected error is stored as a
    short message. The log does not include the message body or the values.
    """
    try:
        parsed = UUID(str(job_id))
    except (AttributeError, TypeError, ValueError):
        logger.warning("match_form_template skipped invalid id")
        return
    try:
        ready = _prepare(parsed)
    except Exception as exc:
        logger.warning(
            "match_form_template failed fill_job=%s error_type=%s",
            parsed,
            type(exc).__name__,
        )
        _fail(parsed)
        return
    if ready is None:
        return
    try:
        raw = await get_llm().complete(system=ready.system, user=ready.user_prompt)
    except Exception as exc:
        logger.warning(
            "match_form_template failed fill_job=%s error_type=%s",
            parsed,
            type(exc).__name__,
        )
        _fail(parsed)
        return
    try:
        enqueue = _save_extraction(parsed, ready, raw)
    except Exception as exc:
        logger.warning(
            "match_form_template failed fill_job=%s error_type=%s",
            parsed,
            type(exc).__name__,
        )
        _fail(parsed)
        return
    if not enqueue:
        return
    try:
        await _enqueue_fill(ctx, str(parsed))
    except Exception as exc:
        logger.warning(
            "run_form_fill enqueue failed fill_job=%s error_type=%s",
            parsed,
            type(exc).__name__,
        )


def _prepare(job_id: UUID) -> MatchReady | None:
    with Session(engine) as session:
        job = session.get(FormFillJob, job_id)
        if job is None:
            logger.warning("fill job %s was not found", job_id)
            return None
        job.status = FormFillStatus.running
        job.updated_at = utcnow()
        session.add(job)
        session.commit()

        message = session.get(Message, job.message_id) if job.message_id is not None else None
        if message is None or message.user_id != job.user_id:
            _finish(session, job, status=FormFillStatus.failed, error=_MESSAGE_MISSING, confidence=None)
            return None

        caller_chose = job.template_id is not None
        if caller_chose:
            template = session.get(FormTemplate, job.template_id)
            if template is None or template.user_id != job.user_id:
                _finish(
                    session,
                    job,
                    status=FormFillStatus.failed,
                    error=_TEMPLATE_MISSING,
                    confidence=None,
                )
                return None
            score = 0
        else:
            chosen = _select_template(session, job.user_id, message)
            if chosen is None:
                _finish(session, job, status=FormFillStatus.failed, error=NO_MATCH, confidence=None)
                return None
            template, score = chosen
            job.template_id = template.id

        _kind, _count, public = schema_summary(template.form_schema)
        text = message_text(message.subject, message.body_text, message.body_preview)
        system, prompt = build_extract_prompts(public["fields"], text)
        confidence = 0.5 if caller_chose else min(1.0, score / 4)
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
        return MatchReady(
            template_id=template.id,
            confidence=confidence,
            system=system,
            user_prompt=prompt,
        )


def _select_template(
    session: Session,
    user_id: UUID,
    message: Message,
) -> tuple[FormTemplate, int] | None:
    rows = session.exec(select(FormTemplate).where(FormTemplate.user_id == user_id)).all()
    haystack = tokens(message_text(message.subject, message.body_text, message.body_preview))
    candidates: list[FormTemplate] = []
    scores: list[int] = []
    for row in rows:
        kind, _count, _public = schema_summary(row.form_schema)
        if kind != "acroform":
            continue
        candidates.append(row)
        scores.append(
            len(template_tokens(row.filename, list(row.tags or []), row.description) & haystack)
        )
    winner = choose_score(scores)
    if winner is None:
        return None
    return candidates[winner], scores[winner]


def _save_extraction(job_id: UUID, ready: MatchReady, raw: str) -> bool:
    extracted = parse_extracted_object(raw)
    with Session(engine) as session:
        job = session.get(FormFillJob, job_id)
        if job is None:
            logger.warning("fill job %s was not found", job_id)
            return False
        job.template_id = ready.template_id
        if extracted is None:
            job.extracted_values = {}
            flag_modified(job, "extracted_values")
            job.dropped_fields = []
            flag_modified(job, "dropped_fields")
            job.confidence = 0
            job.status = FormFillStatus.needs_review
            job.error = None
            job.filled_file_path = None
            job.updated_at = utcnow()
            session.add(job)
            session.commit()
            return False
        job.extracted_values = extracted
        flag_modified(job, "extracted_values")
        job.confidence = ready.confidence
        job.status = FormFillStatus.pending
        job.error = None
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
        return True


def _finish(
    session: Session,
    job: FormFillJob,
    *,
    status: FormFillStatus,
    error: str | None,
    confidence: float | None,
) -> None:
    job.status = status
    job.error = error
    if confidence is not None:
        job.confidence = confidence
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def _fail(job_id: UUID) -> None:
    with Session(engine) as session:
        job = session.get(FormFillJob, job_id)
        if job is None:
            return
        job.status = FormFillStatus.failed
        job.error = MATCH_FAILED
        job.updated_at = utcnow()
        session.add(job)
        session.commit()


async def _enqueue_fill(ctx: dict, job_id: str) -> None:
    redis = ctx.get("redis") if isinstance(ctx, dict) else None
    enqueue = getattr(redis, "enqueue_job", None)
    if enqueue is None:
        logger.info("run_form_fill not enqueued; no redis pool fill_job=%s", job_id)
        return
    await enqueue("run_form_fill", job_id)
