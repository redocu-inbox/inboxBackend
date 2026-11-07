"""Turn Microsoft Graph body HTML into plain text.

Graph `body.content` is often HTML. The API stores and returns plain text only.
Tags, comments, and the contents of script, style, and noscript are dropped.
A value that is empty after that becomes null so the client can keep showing
`body_preview`. Angle brackets never survive, including when Graph mislabels
HTML as text.
"""

from html.parser import HTMLParser
import re

# Long newsletters should not become multi-megabyte JSON rows.
MAX_PLAIN_TEXT_CHARS = 100_000

_SKIP_TAGS = {"script", "style", "noscript"}
_BREAK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "blockquote", "pre"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _SKIP_TAGS:
            self._skip += 1
        if tag in _BREAK_TAGS and self._skip == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in _BREAK_TAGS and self._skip == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data:
            self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        del data


def to_plain_text(value: object) -> str | None:
    """Return plain text, or None when there is nothing safe to show."""
    if not isinstance(value, str):
        return None
    raw = value.replace("\x00", "")
    if not raw.strip():
        return None
    extractor = _TextExtractor()
    try:
        extractor.feed(raw)
        extractor.close()
    except Exception:
        return None
    text = "".join(extractor.parts)
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace("<", "").replace(">", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_PLAIN_TEXT_CHARS:
        text = text[:MAX_PLAIN_TEXT_CHARS].rstrip()
    return text or None
