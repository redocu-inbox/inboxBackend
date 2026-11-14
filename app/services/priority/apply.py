"""Apply the rule classifier and the rule scorer to one stored message.

A classifier failure leaves the classification columns alone and scoring still
runs from whatever is already stored. A scorer failure leaves the priority
columns alone. Neither failure is a mailbox outage.
"""

import logging
from datetime import datetime
from typing import Any

from app.models.user import utcnow
from app.services.priority.classify import classify
from app.services.priority.score import score

logger = logging.getLogger(__name__)


def classify_and_score(message: Any, now: datetime | None = None) -> bool:
    """Classify, then score. Return whether a score was written."""
    moment = utcnow() if now is None else now
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
        message.category = result["category"]
        message.category_source = "rule"
        message.category_confidence = result["category_confidence"]
        message.project_name = result["project_name"]
        message.project_number = result["project_number"]
        message.due_date = result["due_date"]
        message.responsible_party = result["responsible_party"]
        message.reference_number = result["reference_number"]
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
