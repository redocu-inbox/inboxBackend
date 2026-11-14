"""Deterministic construction-mail classifier.

Reads stored subject, preview, body, and From line. It does not take a clock,
write the database, or call an LLM. The worker stores `category_source`.
"""

import re
from datetime import date
from typing import Mapping

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
    return re.compile(rf"(?i)(?<![A-Za-z])(?:{cue})\s*#?\s*{_NUMBER}(?![A-Za-z0-9])")


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

_PROJECT_CUES = (
    re.compile(
        r"(?i)(?<![A-Za-z])project\s+number\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z])project\s+no\.\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z])project\s+no(?![A-Za-z.])\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z])project\s+#\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z])job\s+number\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"
    ),
    re.compile(r"(?i)(?<![A-Za-z])job\s+no\.\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"),
    re.compile(
        r"(?i)(?<![A-Za-z])job\s+no(?![A-Za-z.])\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"
    ),
    re.compile(r"(?i)(?<![A-Za-z])job\s+#\s*[:\-]?\s+([A-Za-z0-9][A-Za-z0-9./_-]{1,31})"),
)

_PROJECT_NAME = re.compile(r"(?i)(?<![A-Za-z])project\s+name\s*[:\-]\s*(.*)$")

_DATE_CUES = (
    "response needed by",
    "due date",
    "due by",
    "due on",
    "respond by",
    "needed by",
    "deadline",
)

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
    return {
        "category": category,
        "category_confidence": confidence,
        "project_name": _project_name(plain),
        "project_number": _project_number(plain),
        "due_date": _due_date(plain),
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


def _due_date(surfaces: tuple[str, str, str]) -> date | None:
    for text in surfaces:
        found = _first_date(text)
        if found is not None:
            return found
    return None


def _first_date(text: str) -> date | None:
    if not text:
        return None
    found: list[tuple[int, date]] = []
    for cue in _DATE_CUES:
        pattern = re.compile(rf"(?i)(?<![A-Za-z]){re.escape(cue)}(?![A-Za-z])")
        for match in pattern.finditer(text):
            index = _skip_separators(text, match.end())
            parsed = _parse_date_at(text, index)
            if parsed is not None:
                found.append((match.start(), parsed))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]


def _skip_separators(text: str, index: int) -> int:
    end = min(len(text), index + 20)
    while index < end and text[index] in " :-":
        index += 1
    return index


def _parse_date_at(text: str, index: int) -> date | None:
    chunk = text[index:]
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", chunk)
    if iso:
        return _valid(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    slash4 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", chunk)
    if slash4:
        return _valid(int(slash4.group(3)), int(slash4.group(1)), int(slash4.group(2)))
    slash2 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})", chunk)
    if slash2:
        year = int(slash2.group(3))
        year = 2000 + year if year <= 69 else 1900 + year
        return _valid(year, int(slash2.group(1)), int(slash2.group(2)))
    month = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", chunk)
    if month is None:
        return None
    month_num = _MONTHS.get(month.group(1).lower())
    if month_num is None:
        return None
    return _valid(int(month.group(3)), month_num, int(month.group(2)))


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
