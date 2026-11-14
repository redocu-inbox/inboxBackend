"""Deterministic construction-mail classifier.

Reads stored subject, preview, body, and From line. It does not take a scoring
clock, write the database, or call an LLM. Relative days use the received
timestamp's calendar date in the scoring timezone. The worker stores
`category_source`.
"""

import re
from datetime import date, timedelta
from typing import Mapping

from app.services.priority.clock import scoring_date
from app.services.priority.text import (
    any_phrase,
    body_slice,
    compile_phrase,
    normalize,
    role_text,
)

_PRECEDENCE = (
    "safety",
    "change_order",
    "rfi",
    "submittal",
    "invoice",
    "schedule",
    "meeting_notes",
)

_CO_HASH = re.compile(r"(?<![a-z])co\s*#\s*[0-9]{1,12}[a-z]?(?![a-z])")

_CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "safety": (
        compile_phrase("stop work"),
        compile_phrase("osha"),
        compile_phrase("near miss"),
    ),
    "change_order": (
        compile_phrase("change order"),
        compile_phrase("potential change order"),
        compile_phrase("pco"),
        _CO_HASH,
    ),
    "rfi": (compile_phrase("rfi"),),
    "submittal": (compile_phrase("submittal"),),
    "invoice": (
        compile_phrase("invoice"),
        compile_phrase("pay app"),
        compile_phrase("pay application"),
        compile_phrase("application for payment"),
    ),
    "schedule": (
        compile_phrase("look ahead"),
        compile_phrase("schedule update"),
        compile_phrase("updated schedule"),
        compile_phrase("project schedule"),
        compile_phrase("construction schedule"),
        compile_phrase("baseline schedule"),
    ),
    "meeting_notes": (
        compile_phrase("meeting minutes"),
        compile_phrase("meeting notes"),
        compile_phrase("minutes of the meeting"),
        compile_phrase("minutes of meeting"),
    ),
}

_NUMBER = r"([0-9]{1,12}[A-Za-z]?)"

def _numbered(cue: str) -> re.Pattern[str]:
    """Number after a cue: `RFI 12`, `RFI #042`, `RFI-052`, or `RFI No. 52`."""
    return re.compile(
        rf"(?i)(?<![A-Za-z])(?:{cue})"
        rf"(?:\s*#\s*|\s*-\s*|\s+no\.?\s*|\s+number\s*[:#\-]?\s*|\s+)"
        rf"{_NUMBER}(?![A-Za-z0-9])"
    )


_REFERENCE_CUES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "change_order": (
        ("CO", re.compile(rf"(?i)(?<![A-Za-z])co\s*#\s*{_NUMBER}(?![A-Za-z0-9])")),
        ("PCO", _numbered(r"pcos?")),
        ("CO", _numbered(r"change\s+orders?")),
    ),
    "rfi": (("RFI", _numbered(r"rfis?")),),
    "submittal": (("Submittal", _numbered(r"submittals?")),),
    "invoice": (
        ("Invoice", _numbered(r"invoices?")),
        ("Pay App", _numbered(r"pay\s+app")),
        ("Pay App", _numbered(r"pay\s+applications?")),
        ("Pay App", _numbered(r"application\s+for\s+payments?")),
    ),
}

_PROJECT_VALUE = r"([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"


def _project_cue(prefix: str) -> re.Pattern[str]:
    """Label, optional colon or hyphen, then the number. The space is optional."""
    return re.compile(
        rf"(?i)(?<![A-Za-z]){prefix}\s*[:\-]?\s*{_PROJECT_VALUE}"
    )


_PROJECT_CUES = (
    _project_cue(r"project\s+number"),
    _project_cue(r"project\s+no\."),
    _project_cue(r"project\s+no(?![A-Za-z.])"),
    _project_cue(r"project\s+#"),
    _project_cue(r"job\s+number"),
    _project_cue(r"job\s+no\."),
    _project_cue(r"job\s+no(?![A-Za-z.])"),
    _project_cue(r"job\s+#"),
)

_PROJECT_NAME = re.compile(r"(?i)(?<![A-Za-z])project\s+name\s*[:\-]\s*(.*)$")

_DATE_CUES = (
    "response needed by",
    "response due",
    "due date",
    "due by",
    "due on",
    "respond by",
    "needed by",
    "deadline",
    "due",
)

# A yearless month/day more than this many days before the received date rolls forward.
_YEAR_LOOKBACK_DAYS = 30
_YEAR_LOOKAHEAD_DAYS = 366

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_RELATIVE_WORD = (
    r"(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
)
_RELATIVE_AT = re.compile(
    rf"(?i)^(?:(last|next)\s+)?(?:\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?\s+)?({_RELATIVE_WORD})\b"
)
# "by Friday" is a due date only after a deadline word. "drop by today" is not.
_DEADLINE_WORD = (
    r"due|deadline|respond|response|needed|need|required|submit|return|"
    r"complete|confirm|reply|advise|send"
)
_DEADLINE_BY = re.compile(
    rf"(?i)(?<![A-Za-z])(?:{_DEADLINE_WORD})\s+by\s+"
    rf"(?:(last|next)\s+)?(?:\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?\s+)?({_RELATIVE_WORD})\b"
)
_DUE_WORD = re.compile(r"(?i)(?<![A-Za-z])due(?![A-Za-z])(?!\s+to\b)")
_COMPILED_DATE_CUES = tuple(
    (cue, _DUE_WORD if cue == "due" else re.compile(rf"(?i)(?<![A-Za-z]){re.escape(cue)}(?![A-Za-z])"))
    for cue in _DATE_CUES
)
_EVENT_WORD = re.compile(
    r"(?i)(?<![A-Za-z])(meetings?|showings?|pickups?|inspections?)\b"
)
_NEAR_DATE = re.compile(
    rf"(?i)(?:(?<![A-Za-z])((?:last|next)\s+{_RELATIVE_WORD}|{_RELATIVE_WORD})\b)"
    rf"|(?:(?<!\d)(\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?)(?!/))"
    rf"|(?:(?<![A-Za-z])((?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    rf"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    rf"nov(?:ember)?|dec(?:ember)?)\s+\d{{1,2}}(?:,\s*\d{{4}})?)\b)"
)
_DEADLINE_BETWEEN = re.compile(
    r"(?i)(?<![A-Za-z])(?:due|deadline|respond|response)(?![A-Za-z])"
)
_EVENT_WINDOW = 48

_ROLES = (
    (compile_phrase("project manager"), "Project manager"),
    (compile_phrase("superintendent"), "Superintendent"),
    (compile_phrase("estimator"), "Estimator"),
    (compile_phrase("architect"), "Architect"),
    (compile_phrase("owners rep"), "Owner's rep"),
)


def classify(fields: Mapping[str, object]) -> dict[str, object]:
    """Return the category, confidence, and extracted fields. Source is the worker's job."""
    parts = _normalized_parts(fields)
    matched = [
        name
        for name in _PRECEDENCE
        if any_phrase(_CATEGORY_PATTERNS[name], parts)
    ]
    if not matched:
        category = None
        confidence = 0.0
    elif len(matched) == 1:
        category = matched[0]
        confidence = 1.0
    else:
        category = matched[0]
        confidence = 0.5
    plain = _plain_surfaces(fields)
    received = _received_date(fields.get("received_at"))
    return {
        "category": category,
        "category_confidence": confidence,
        "project_name": _project_name(plain),
        "project_number": _project_number(plain),
        "due_date": _due_date(plain, received),
        "responsible_party": _responsible_party(fields),
        "reference_number": _reference(category, plain),
    }


def _normalized_parts(fields: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        normalize(fields.get("subject")),
        normalize(fields.get("body_preview")),
        normalize(body_slice(fields.get("body_text"))),
    )


def _plain_surfaces(fields: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        _plain(fields.get("subject")),
        body_slice(fields.get("body_text")),
        _plain(fields.get("body_preview")),
    )


def _plain(value: object) -> str:
    return value if isinstance(value, str) else ""


def _reference(category: str | None, surfaces: tuple[str, str, str]) -> str | None:
    cues = _REFERENCE_CUES.get(category or "")
    if not cues:
        return None
    for text in surfaces:
        if not text:
            continue
        for label, pattern in cues:
            match = pattern.search(text)
            if match:
                return f"{label} {match.group(1)}"
    return None


def _project_number(surfaces: tuple[str, str, str]) -> str | None:
    for text in surfaces:
        if not text:
            continue
        for pattern in _PROJECT_CUES:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return None


def _project_name(surfaces: tuple[str, str, str]) -> str | None:
    for text in surfaces:
        if not text:
            continue
        for line in text.splitlines():
            match = _PROJECT_NAME.search(line)
            if match is None:
                continue
            name = re.sub(r"\s+", " ", match.group(1)).strip()
            if name:
                return name[:120]
    return None


def mentioned_dates(fields: Mapping[str, object]) -> list[date]:
    """Calendar dates and relative days in the message, anchored to received_at."""
    received = _received_date(fields.get("received_at"))
    found: list[date] = []
    for text in _plain_surfaces(fields):
        if not text:
            continue
        calendar = _first_date(text, None)
        if calendar is not None:
            found.append(calendar)
        if received is None:
            continue
        for match in re.finditer(
            rf"(?i)(?<![A-Za-z])(?:(last|next)\s+)?({_RELATIVE_WORD})\b",
            text,
        ):
            resolved = _relative_from_match(match.group(1), match.group(2), received)
            if resolved is not None:
                found.append(resolved)
    return found


def event_dates(fields: Mapping[str, object], *, meeting: bool = True) -> list[date]:
    """Dates that sit next to a meeting, showing, pickup, or inspection.

    `meeting=False` ignores the word meeting, which is how minutes are kept
    out of the happening-soon signal while an inspection in the same mail can
    still count. A deadline word between the event and the date does not count.
    """
    received = _received_date(fields.get("received_at"))
    found: list[date] = []
    for text in _plain_surfaces(fields):
        if not text:
            continue
        events = [
            match
            for match in _EVENT_WORD.finditer(text)
            if meeting or not match.group(1).lower().startswith("meeting")
        ]
        if not events:
            continue
        for token in _NEAR_DATE.finditer(text):
            resolved = _date_from_token(token.group(0), received)
            if resolved is None:
                continue
            for event in events:
                if not _spans_close(event.start(), event.end(), token.start(), token.end()):
                    continue
                left = min(event.end(), token.end())
                right = max(event.start(), token.start())
                if right > left and _DEADLINE_BETWEEN.search(text[left:right]):
                    continue
                found.append(resolved)
                break
    return found


def _spans_close(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    if left_end <= right_start:
        return right_start - left_end <= _EVENT_WINDOW
    if right_end <= left_start:
        return left_start - right_end <= _EVENT_WINDOW
    return True


def _due_date(surfaces: tuple[str, str, str], received: date | None) -> date | None:
    for text in surfaces:
        found = _first_date(text, received)
        if found is not None:
            return found
    return None


def _first_date(text: str, received: date | None) -> date | None:
    if not text:
        return None
    found: list[tuple[int, date]] = []
    for _cue, pattern in _COMPILED_DATE_CUES:
        for match in pattern.finditer(text):
            index = _skip_separators(text, match.end())
            parsed = _parse_date_at(text, index, received)
            if parsed is not None:
                found.append((match.start(), parsed))
    if received is not None:
        for match in _DEADLINE_BY.finditer(text):
            resolved = _relative_from_match(match.group(1), match.group(2), received)
            if resolved is not None:
                found.append((match.start(), resolved))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]


def _skip_separators(text: str, index: int) -> int:
    end = min(len(text), index + 20)
    while index < end and text[index] in " :-":
        index += 1
    return index


def _parse_date_at(text: str, index: int, received: date | None) -> date | None:
    chunk = text[index:]
    absolute = _parse_absolute(chunk, received)
    if absolute is not None:
        return absolute
    lead = re.match(
        rf"(?i)^(?:(last|next)\s+)?({_RELATIVE_WORD})\b[\s,:-]+",
        chunk,
    )
    if lead is not None:
        absolute = _parse_absolute(chunk[lead.end() :], received)
        if absolute is not None:
            return absolute
        if received is not None:
            return _relative_from_match(lead.group(1), lead.group(2), received)
    return _relative_at(chunk, received)


def _parse_absolute(chunk: str, received: date | None) -> date | None:
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})\b", chunk)
    if iso:
        return _valid(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    slash4 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\b", chunk)
    if slash4:
        return _valid(int(slash4.group(3)), int(slash4.group(1)), int(slash4.group(2)))
    slash2 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})\b", chunk)
    if slash2:
        year = int(slash2.group(3))
        year = 2000 + year if year <= 69 else 1900 + year
        return _valid(year, int(slash2.group(1)), int(slash2.group(2)))
    month = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", chunk)
    if month is not None:
        month_num = _MONTHS.get(month.group(1).lower())
        if month_num is not None:
            return _valid(int(month.group(3)), month_num, int(month.group(2)))
    month_only = re.match(r"([A-Za-z]+)\s+(\d{1,2})\b(?!\s*,?\s*\d)", chunk)
    if month_only is not None:
        month_num = _MONTHS.get(month_only.group(1).lower())
        if month_num is not None:
            return _infer_year(month_num, int(month_only.group(2)), received)
    slash_md = re.match(r"(\d{1,2})/(\d{1,2})(?!/)", chunk)
    if slash_md:
        return _infer_year(int(slash_md.group(1)), int(slash_md.group(2)), received)
    return None


def _infer_year(month: int, day: int, received: date | None) -> date | None:
    """Nearest month/day to `received`, allowing a short overdue window.

    A date with no year is the occurrence within 30 days before the received
    date or 366 days after it. `1/5` received on December 30 is next year.
    `12/30` received on January 3 stays in the year that just ended.
    """
    if received is None:
        return None
    candidates: list[date] = []
    for year in (received.year - 1, received.year, received.year + 1):
        parsed = _valid(year, month, day)
        if parsed is not None:
            candidates.append(parsed)
    pool = [
        item
        for item in candidates
        if -_YEAR_LOOKBACK_DAYS <= (item - received).days <= _YEAR_LOOKAHEAD_DAYS
    ]
    if not pool:
        return None
    pool.sort(key=lambda item: (abs((item - received).days), 0 if item >= received else 1))
    return pool[0]


def _date_from_token(token: str, received: date | None) -> date | None:
    relative = re.match(rf"(?i)^(?:(last|next)\s+)?({_RELATIVE_WORD})$", token.strip())
    if relative:
        if received is None:
            return None
        return _relative_from_match(relative.group(1), relative.group(2), received)
    return _parse_absolute(token.strip(), received)


def _relative_at(chunk: str, received: date | None) -> date | None:
    if received is None:
        return None
    match = _RELATIVE_AT.match(chunk)
    if match is None:
        return None
    return _relative_from_match(match.group(1), match.group(2), received)


def _relative_from_match(kind: str | None, word: str, received: date) -> date | None:
    label = (kind or "").lower()
    if label == "last":
        direction = "back"
    elif label == "next":
        direction = "next"
    else:
        direction = "forward"
    return _relative_day(word, received, direction=direction)


def _relative_day(word: str, received: date, *, direction: str = "forward") -> date | None:
    name = word.lower()
    if name == "today":
        return None if direction == "back" else received
    if name == "tomorrow":
        return None if direction == "back" else received + timedelta(days=1)
    weekday = _WEEKDAYS.get(name)
    if weekday is None:
        return None
    if direction == "back":
        delta = (received.weekday() - weekday) % 7
        if delta == 0:
            delta = 7
        return received - timedelta(days=delta)
    delta = (weekday - received.weekday()) % 7
    if direction == "next" and delta == 0:
        delta = 7
    return received + timedelta(days=delta)


def _received_date(value: object) -> date | None:
    return scoring_date(value)


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _responsible_party(fields: Mapping[str, object]) -> str | None:
    parts = (
        role_text(fields.get("from_name")),
        role_text(_local_part(fields.get("from_email"))),
    )
    for pattern, label in _ROLES:
        if any(part and pattern.search(part) for part in parts):
            return label
    return None


def _local_part(value: object) -> str:
    if not isinstance(value, str) or "@" not in value:
        return ""
    return value.split("@", 1)[0]
