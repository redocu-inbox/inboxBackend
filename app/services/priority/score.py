"""Deterministic priority score from stored message columns.

No network and no LLM. Tests pass `now` and, when they need it, a VIP domain tuple.
Production uses the empty VIP tuple until real domains are added after a live mailbox test.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

from app.services.priority.classify import event_dates
from app.services.priority.clock import scoring_date
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

# 20 keeps a direct ask plus recent plus unread at the medium line (40).
# RFI and change order are 22, so those keywords still rank above the ask.
ASKS_YOU = ("asks_you", "Asks something of you", 20)
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
            compile_phrase("response due"),
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

# "let me know", "could you", and "would you" are closings, not asks.
_ASK_PHRASES = (
    compile_phrase("please send"),
    compile_phrase("can you confirm"),
    compile_phrase("please confirm"),
    compile_phrase("sign and return"),
    compile_phrase("please reply"),
    compile_phrase("please advise"),
    compile_phrase("action required"),
)

# "follow up" matches thank-you notes. "following up" is the repeat.
_REPEAT_PHRASES = (
    compile_phrase("final notice"),
    compile_phrase("second request"),
    compile_phrase("third request"),
    compile_phrase("following up"),
    compile_phrase("second notice"),
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

_MINUTES_PHRASES = (
    compile_phrase("meeting minutes"),
    compile_phrase("meeting notes"),
    compile_phrase("minutes of the meeting"),
    compile_phrase("minutes of meeting"),
)

_CONSTRUCTION_CATEGORIES = frozenset({"rfi", "change_order", "submittal", "safety"})
_CONSTRUCTION_REFERENCE = re.compile(r"(?i)^(RFI|CO|PCO|Submittal)\b")
_CONSTRUCTION_PHRASES = (
    compile_phrase("rfi"),
    compile_phrase("change order"),
    compile_phrase("potential change order"),
    compile_phrase("pco"),
    compile_phrase("submittal"),
    compile_phrase("stop work"),
    compile_phrase("osha"),
    compile_phrase("near miss"),
)

# Notifications from these domains are project mail even when the address is no-reply.
_PLATFORM_DOMAINS = (
    "procore.com",
    "autodesk.com",
    "plangrid.com",
    "buildertrend.com",
    "bluebeam.com",
    "aconex.com",
    "oraclecloud.com",
)

_SIGNOFF_LINE = re.compile(
    r"(?i)^(?:thanks|thank you|many thanks|regards|best regards|kind regards|"
    r"warm regards|sincerely|cheers|best|all the best)\s*,?\s*$"
)
_BOILERPLATE_LINE = re.compile(
    r"(?i)^(?:"
    r"--\s*$|"
    r"--\s|"
    r"_{5,}\s*$|"
    r"-{5,}.*original message.*$|"
    r"sent from my (?:iphone|ipad|android|samsung|mobile).*$|"
    r"get outlook for .*$|"
    r"on .+wrote:\s*$|"
    r">.*"
    r")$"
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

    ask_parts = _ask_parts(fields)
    if any_phrase(_ASK_PHRASES, ask_parts) or _subject_asks(fields.get("subject")):
        reasons.append(ASKS_YOU)
    if any_phrase(_REPEAT_PHRASES, ask_parts):
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
    today = scoring_date(moment)
    if due is not None and today is not None:
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


def _ask_parts(fields: Mapping[str, object]) -> tuple[str, str, str]:
    """Subject plus body text with the signature and quoted reply cut off."""
    return (
        normalize(fields.get("subject")),
        normalize(_without_boilerplate(fields.get("body_preview"))),
        normalize(_without_boilerplate(fields.get("body_text"))),
    )


def _without_boilerplate(value: object) -> str:
    text = body_slice(value)
    if not text:
        return ""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _SIGNOFF_LINE.match(stripped) or _BOILERPLATE_LINE.match(stripped):
            break
        kept.append(line)
    return "\n".join(kept)


def _happening_soon(
    fields: Mapping[str, object],
    keyword_parts: tuple[str, str, str],
    moment: datetime | None,
) -> bool:
    today = scoring_date(moment)
    if today is None:
        return False
    window = {today, today + timedelta(days=1)}
    minutes = fields.get("category") == "meeting_notes" or any_phrase(
        _MINUTES_PHRASES, keyword_parts
    )
    return any(item in window for item in event_dates(fields, meeting=not minutes))


def _bulk_reason(
    fields: Mapping[str, object],
    keyword_parts: tuple[str, str, str],
) -> tuple[str, str, int] | None:
    # Slack has no From address. Phrases like "do not reply" show up in normal chat.
    if fields.get("provider") == "slack":
        return None
    if _construction_mail(fields, keyword_parts) or _platform_sender(fields.get("from_email")):
        return None
    local = _local_part(fields.get("from_email")).lower()
    compact = local.replace(".", "").replace("_", "").replace("-", "")
    if "noreply" in compact or "donotreply" in compact or "mailerdaemon" in compact:
        return BULK_NO_REPLY
    if any_phrase(_BULK_PHRASES, keyword_parts):
        return BULK_AUTOMATED
    return None


def _construction_mail(
    fields: Mapping[str, object],
    keyword_parts: tuple[str, str, str],
) -> bool:
    category = fields.get("category")
    if isinstance(category, str) and category in _CONSTRUCTION_CATEGORIES:
        return True
    reference = fields.get("reference_number")
    if isinstance(reference, str) and _CONSTRUCTION_REFERENCE.match(reference.strip()):
        return True
    return any_phrase(_CONSTRUCTION_PHRASES, keyword_parts)


def _platform_sender(value: object) -> bool:
    domain = _domain(value)
    if not domain:
        return False
    return any(domain == item or domain.endswith("." + item) for item in _PLATFORM_DOMAINS)


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return scoring_date(value)
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
