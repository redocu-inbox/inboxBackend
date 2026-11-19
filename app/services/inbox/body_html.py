"""Sanitize untrusted email HTML for the inbox reading pane."""

from urllib.parse import urlsplit

import nh3

from app.services.graph.html_text import to_plain_text

_MAX_HTML_CHARS = 200_000
_LINK_SCHEMES = {"http", "https"}


def _allow_link(_tag: str, attribute: str, value: str) -> str | None:
    if attribute != "href":
        return value
    try:
        url = urlsplit(value)
        if url.scheme in _LINK_SCHEMES and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


_CLEANER = nh3.Cleaner(
    tags={
        "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4",
        "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "strong", "table",
        "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
    },
    clean_content_tags={"script", "style", "noscript", "iframe", "object", "template", "svg", "math"},
    attributes={"a": {"href"}},
    attribute_filter=_allow_link,
    url_schemes=_LINK_SCHEMES,
    url_relative="deny",
)


def sanitize_body_html(value: str | None) -> str | None:
    """Keep readable markup without any resources that load on render."""
    if not value or len(value) > _MAX_HTML_CHARS:
        return None
    try:
        cleaned = _CLEANER.clean(value)
    except UnicodeError:
        return None
    if len(cleaned) > _MAX_HTML_CHARS or not to_plain_text(cleaned):
        return None
    return cleaned
