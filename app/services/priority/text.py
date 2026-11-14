"""Shared text normalization for the rule scorer and the rule classifier."""

import re

_TAG = re.compile(r"<[^>]*>")
_WHITESPACE = re.compile(r"\s+")

BODY_SCAN_LIMIT = 8000


def body_slice(value: object) -> str:
    """The first 8000 characters of body text. Non-strings are empty."""
    if not isinstance(value, str):
        return ""
    return value[:BODY_SCAN_LIMIT]


def normalize(value: object) -> str:
    """Lowercase keyword text. Tags, hyphens, and apostrophes do not hide a word."""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = _TAG.sub(" ", value)
    text = text.replace("<", " ").replace(">", " ")
    text = text.lower().replace("-", " ").replace("'", "").replace("’", "")
    text = _WHITESPACE.sub(" ", text).strip()
    return text


def role_text(value: object) -> str:
    """From-line text. Dots and underscores are spaces, then the keyword normalizer."""
    if not isinstance(value, str):
        return ""
    return normalize(value.replace(".", " ").replace("_", " "))


def compile_phrase(phrase: str) -> re.Pattern[str]:
    """A phrase with a non-letter boundary on each side. The last word may end in one s."""
    words = phrase.split(" ")
    pieces: list[str] = []
    for index, word in enumerate(words):
        token = re.escape(word)
        if index == len(words) - 1:
            token += "s?"
        pieces.append(token)
    body = r"\s+".join(pieces)
    return re.compile(rf"(?<![a-z]){body}(?![a-z])")


def any_phrase(patterns: tuple[re.Pattern[str], ...], parts: tuple[str, ...]) -> bool:
    return any(pattern.search(part) for part in parts if part for pattern in patterns)
