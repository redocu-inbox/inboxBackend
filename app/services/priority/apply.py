"""Apply the rule classifier and the rule scorer to one stored message.

A classifier failure leaves the classification columns alone and scoring still
runs from whatever is already stored. A scorer failure leaves the priority
columns alone. Neither failure is a mailbox outage.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from app.models.classification_correction import MessageClassificationCorrection
from app.models.user import utcnow
from app.services.priority.classify import classify
from app.services.priority.score import score

logger = logging.getLogger(__name__)

_EXTRACTED_FIELDS = (
    "project_name",
    "project_number",
    "due_date",
    "responsible_party",
    "reference_number",
)


def locked_fields(session: Session, message_ids: list[UUID]) -> dict[UUID, set[str]]:
    """Fields a user has corrected. Sync and re-score leave those stored values."""
    if not message_ids:
        return {}
    rows = session.exec(
        select(
            MessageClassificationCorrection.message_id,
            MessageClassificationCorrection.field,
        ).where(MessageClassificationCorrection.message_id.in_(message_ids))
    ).all()
    found = {message_id: set() for message_id in message_ids}
    for message_id, field in rows:
        found.setdefault(message_id, set()).add(field)
    return found


def classify_and_score(
    message: Any,
    now: datetime | None = None,
    locked: set[str] | None = None,
) -> bool:
    """Classify, then score. Return whether a score was written.

    `locked` names fields that already have a correction. Those stay as stored,
    including category source and confidence when `category` is locked.
    """
    moment = utcnow() if now is None else now
    held = locked or set()
    fields = _fields(message)
    try:
        result = classify(fields)
    except Exception as exc:
        logger.warning(
            "classify failed message=%s error_type=%s",
            getattr(message, "id", None),
            type(exc).__name__,
        )
    else:
        if "category" not in held:
            message.category = result["category"]
            message.category_source = "rule"
            message.category_confidence = result["category_confidence"]
        for name in _EXTRACTED_FIELDS:
            if name not in held:
                setattr(message, name, result[name])
        fields = _fields(message)
    try:
        scored, band, reasons, stamp = score(fields, moment)
    except Exception as exc:
        logger.warning(
            "score failed message=%s error_type=%s",
            getattr(message, "id", None),
            type(exc).__name__,
        )
        return False
    message.priority_score = scored
    message.priority_band = band
    message.priority_reasons = reasons
    message.priority_scored_at = stamp
    return True


def message_fields(message: Any) -> dict[str, object]:
    """Stored columns the scorer reads."""
    return _fields(message)


def _fields(message: Any) -> dict[str, object]:
    values = {
        "subject": getattr(message, "subject", None),
        "body_preview": getattr(message, "body_preview", None),
        "body_text": getattr(message, "body_text", None),
        "from_name": getattr(message, "from_name", None),
        "from_email": getattr(message, "from_email", None),
        "is_read": getattr(message, "is_read", None),
        "received_at": getattr(message, "received_at", None),
        "category": getattr(message, "category", None),
        "due_date": getattr(message, "due_date", None),
    }
    return values
