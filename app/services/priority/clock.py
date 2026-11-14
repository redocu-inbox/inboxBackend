"""Calendar dates for scoring, in the configured zone.

Relative words and due/overdue use a civil date. UTC makes a US evening look
like the next day. The default zone is US Eastern. `tzdata` supplies the zone
when the image has no system zoneinfo, which is the case for python:3.12-slim.
"""

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_SCORING_TIMEZONE = "America/New_York"
_warned = False


def scoring_zone() -> ZoneInfo:
    """The zone for today, tomorrow, weekdays, and due-date comparison.

    An unknown `SCORING_TIMEZONE` falls back to US Eastern so scoring still runs.
    """
    global _warned
    name = (get_settings().scoring_timezone or "").strip() or DEFAULT_SCORING_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if not _warned:
            logger.warning(
                "SCORING_TIMEZONE %s is invalid; using %s",
                name,
                DEFAULT_SCORING_TIMEZONE,
            )
            _warned = True
        return ZoneInfo(DEFAULT_SCORING_TIMEZONE)


def scoring_date(value: object) -> date | None:
    """The calendar date of `value` in the scoring zone.

    Aware datetimes convert. Naive datetimes are UTC, matching stored provider
    timestamps that omitted an offset. A bare `date` is already a calendar day.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(scoring_zone()).date()
    if isinstance(value, date):
        return value
    return None
