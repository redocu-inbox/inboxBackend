"""Deterministic priority score from stored message columns.

No network and no LLM. Tests pass `now` and, when they need it, a VIP domain tuple.
Production uses the empty VIP tuple until real domains are added after a live mailbox test.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

from app.services.priority.classify import mentioned_dates
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

ASKS_YOU = ("asks_you", "Asks something of you", 25)
REPEAT_ASK = ("repeat_ask", "Repeat ask", 15)
HAPPENING_SOON = ("happening_soon", "Happening today or tomorrow", 15)
ACT_ON_ATTACHMENT = ("act_on_attachment", "Attachment to act on", 8)
BULK_AUTOMATED = ("bulk_automated", "Bulk or automated", -20)
BULK_NO_REPLY = ("bulk_automated", "Bulk or automated", -40)

DUE_OVERDUE = ("due_overdue", "Overdue", 25)
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

_ASK_PHRASES = (
    compile_phrase("please send"),
    compile_phrase("can you confirm"),
    compile_phrase("please confirm"),
    compile_phrase("sign and return"),
    compile_phrase("please reply"),
    compile_phrase("please advise"),
    compile_phrase("let me know"),
    compile_phrase("could you"),
    compile_phrase("would you"),
    compile_phrase("action required"),
)

_REPEAT_PHRASES = (
    compile_phrase("final notice"),
    compile_phrase("second request"),
    compile_phrase("third request"),
    compile_phrase("following up"),
    compile_phrase("follow up"),
    compile_phrase("second notice"),
)

_EVENT_PHRASES = (
    compile_phrase("meeting"),
    compile_phrase("showing"),
    compile_phrase("pickup"),
    compile_phrase("inspection"),
)

_ATTACHMENT_PHRASES = (
    compile_phrase("sign and return"),
    compile_phrase("please sign"),
    compile_phrase("document to sign"),
    compile_phrase("form to sign"),
    compile_phrase("for your signature"),
    compile_phrase("attached for signature"),
)

_BULK_PHRASES = (
    compile_phrase("unsubscribe"),
    compile_phrase("newsletter"),
    compile_phrase("view in browser"),
    compile_phrase("you are receiving this"),
    compile_phrase("your receipt"),
    compile_phrase("this is an automated"),
    compile_phrase("do not reply"),
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

    if any_phrase(_ASK_PHRASES, keyword_parts) or _subject_asks(fields.get("subject")):
        reasons.append(ASKS_YOU)
    if any_phrase(_REPEAT_PHRASES, keyword_parts):
        reasons.append(REPEAT_ASK)
    if any_phrase(_ATTACHMENT_PHRASES, keyword_parts):
        reasons.append(ACT_ON_ATTACHMENT)
    if _happening_soon(fields, keyword_parts, moment):
        reasons.append(HAPPENING_SOON)
    bulk = _bulk_reason(fields, keyword_parts)
    if bulk is not None:
        reasons.append(bulk)

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


def _subject_asks(value: object) -> bool:
    return isinstance(value, str) and "?" in value


def _happening_soon(
    fields: Mapping[str, object],
    keyword_parts: tuple[str, str, str],
    moment: datetime | None,
) -> bool:
    if moment is None or not any_phrase(_EVENT_PHRASES, keyword_parts):
        return False
    today = moment.date()
    tomorrow = today + timedelta(days=1)
    return any(item == today or item == tomorrow for item in mentioned_dates(fields))


def _bulk_reason(
    fields: Mapping[str, object],
    keyword_parts: tuple[str, str, str],
) -> tuple[str, str, int] | None:
    local = _local_part(fields.get("from_email")).lower()
    compact = local.replace(".", "").replace("_", "").replace("-", "")
    if "noreply" in compact or "donotreply" in compact or "mailerdaemon" in compact:
        return BULK_NO_REPLY
    if any_phrase(_BULK_PHRASES, keyword_parts):
        return BULK_AUTOMATED
    return None


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
