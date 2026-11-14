"""Deterministic priority score from stored message columns.

No network and no LLM. Tests pass `now` and, when they need it, a VIP domain tuple.
Production uses the empty VIP tuple until real domains are added after a live mailbox test.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

from app.services.priority.text import any_phrase, body_slice, compile_phrase, normalize, role_text

# Keyword, recency, and sender weights. Category and due-date weights are separate.
KEYWORD_RFI = ("keyword_rfi", "RFI", 22)
KEYWORD_CHANGE_ORDER = ("keyword_change_order", "Change order", 22)
KEYWORD_SAFETY = ("keyword_safety", "Safety", 20)
KEYWORD_DEADLINE = ("keyword_deadline", "Deadline", 16)
KEYWORD_URGENT = ("keyword_urgent", "Marked urgent", 16)
KEYWORD_SUBMITTAL = ("keyword_submittal", "Submittal", 14)
KEYWORD_PAYMENT = ("keyword_payment", "Payment", 12)
RECENT = ("recent", "Received in the last 2 days", 12)
SENDER_VIP = ("sender_vip", "VIP sender", 10)
UNREAD = ("unread", "Unread", 8)
SENDER_PROJECT_ROLE = ("sender_project_role", "Project role", 8)
THIS_WEEK = ("this_week", "Received this week", 6)

DUE_OVERDUE = ("due_overdue", "Overdue", 18)
DUE_SOON = ("due_soon", "Due within 3 days", 14)
DUE_WITHIN_7_DAYS = ("due_within_7_days", "Due within 7 days", 8)

CATEGORY_REASONS = {
    "safety": ("category_safety", "Safety notice", 12),
    "rfi": ("category_rfi", "RFI", 10),
    "change_order": ("category_change_order", "Change order", 10),
    "submittal": ("category_submittal", "Submittal", 8),
    "schedule": ("category_schedule", "Schedule", 8),
    "invoice": ("category_invoice", "Invoice", 6),
}

_KEYWORD_RULES = (
    (KEYWORD_RFI, (compile_phrase("rfi"),)),
    (
        KEYWORD_CHANGE_ORDER,
        (
            compile_phrase("change order"),
            compile_phrase("potential change order"),
            compile_phrase("pco"),
        ),
    ),
    (
        KEYWORD_SAFETY,
        (compile_phrase("stop work"), compile_phrase("osha"), compile_phrase("near miss")),
    ),
    (
        KEYWORD_DEADLINE,
        (
            compile_phrase("deadline"),
            compile_phrase("due by"),
            compile_phrase("due date"),
            compile_phrase("respond by"),
            compile_phrase("response needed"),
        ),
    ),
    (
        KEYWORD_URGENT,
        (compile_phrase("urgent"), compile_phrase("asap"), compile_phrase("time sensitive")),
    ),
    (KEYWORD_SUBMITTAL, (compile_phrase("submittal"),)),
    (
        KEYWORD_PAYMENT,
        (
            compile_phrase("pay app"),
            compile_phrase("pay application"),
            compile_phrase("application for payment"),
            compile_phrase("lien"),
            compile_phrase("past due"),
            compile_phrase("invoice"),
        ),
    ),
)

_ROLE_PHRASES = (
    compile_phrase("project manager"),
    compile_phrase("superintendent"),
    compile_phrase("estimator"),
    compile_phrase("architect"),
    compile_phrase("owners rep"),
)

# Empty until real company domains are added after the live mailbox test.
VIP_DOMAINS: tuple[str, ...] = ()

_RECENT_WINDOW = timedelta(hours=48)
_WEEK_WINDOW = timedelta(days=7)
_FUTURE_SLACK = timedelta(hours=1)


def score(
    fields: Mapping[str, object],
    now: datetime,
    vip_domains: Sequence[str] | None = None,
) -> tuple[float, str, list[dict[str, object]], datetime]:
    """Return the clamped score, the band, the ordered reasons, and `now`."""
    reasons: list[tuple[str, str, int]] = []
    keyword_parts = (
        normalize(fields.get("subject")),
        normalize(fields.get("body_preview")),
        normalize(body_slice(fields.get("body_text"))),
    )
    for signal, patterns in _KEYWORD_RULES:
        if any_phrase(patterns, keyword_parts):
            reasons.append(signal)

    received = _as_utc(fields.get("received_at"))
    moment = _as_utc(now)
    if received is not None and moment is not None:
        delta = received - moment
        if -_RECENT_WINDOW <= delta <= _FUTURE_SLACK:
            reasons.append(RECENT)
        elif -_WEEK_WINDOW <= delta <= _FUTURE_SLACK:
            reasons.append(THIS_WEEK)

    domains = VIP_DOMAINS if vip_domains is None else tuple(vip_domains)
    domain = _domain(fields.get("from_email"))
    if domain and domain in domains:
        reasons.append(SENDER_VIP)

    if fields.get("is_read") is False:
        reasons.append(UNREAD)

    role_parts = (
        role_text(fields.get("from_name")),
        role_text(_local_part(fields.get("from_email"))),
    )
    if any_phrase(_ROLE_PHRASES, role_parts):
        reasons.append(SENDER_PROJECT_ROLE)

    category = fields.get("category")
    if isinstance(category, str) and category in CATEGORY_REASONS:
        reasons.append(CATEGORY_REASONS[category])

    due = _as_date(fields.get("due_date"))
    if due is not None and moment is not None:
        today = moment.date()
        if due < today:
            reasons.append(DUE_OVERDUE)
        elif due <= today + timedelta(days=3):
            reasons.append(DUE_SOON)
        elif due <= today + timedelta(days=7):
            reasons.append(DUE_WITHIN_7_DAYS)

    reasons.sort(key=lambda item: (-item[2], item[0]))
    total = sum(item[2] for item in reasons)
    clamped = float(min(100, max(0, total)))
    if clamped >= 70:
        band = "high"
    elif clamped >= 40:
        band = "medium"
    else:
        band = "low"
    payload = [{"code": code, "label": label, "weight": weight} for code, label, weight in reasons]
    return clamped, band, payload, now


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return _as_utc(value).date() if _as_utc(value) is not None else None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _domain(value: object) -> str:
    if not isinstance(value, str) or "@" not in value:
        return ""
    return value.rsplit("@", 1)[1].lower()


def _local_part(value: object) -> str:
    if not isinstance(value, str) or "@" not in value:
        return ""
    return value.split("@", 1)[0]
