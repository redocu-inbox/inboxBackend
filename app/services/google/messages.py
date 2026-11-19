"""Map Gmail API messages into the columns already used for Outlook mail.

Reads are GET-only. This module does not send, modify, or delete mail.
"""

import base64
import re
from datetime import datetime, timezone

from urllib.error import HTTPError
from urllib.parse import urlencode

from app.services.google.http import GMAIL_INBOX_LIST_URL, gmail_message_url, google_get
from app.services.graph.html_text import to_plain_text
from app.services.inbox.body_html import sanitize_body_html
from app.services.inbox.recipients import header_people

_EMAIL_IN_BRACKETS = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_LIST_BASE = GMAIL_INBOX_LIST_URL.split("?", 1)[0]


class GmailRequestError(Exception):
    """A Gmail HTTP failure. `status` is the code. The body is not stored."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"gmail http {status}")


def _address(value: str) -> str | None:
    if "@" not in value or any(char.isspace() or char in "\r\n\x00<>" for char in value):
        return None
    return value[:320]


def _sender(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    matches = list(_EMAIL_IN_BRACKETS.finditer(value))
    if matches:
        last = matches[-1]
        return to_plain_text(value[: last.start()]), _address(last.group(1))
    return None, _address(value.strip())


def _header(payload: dict, name: str) -> str | None:
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return None
    wanted = name.lower()
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = header.get("name")
        if not isinstance(header_name, str) or header_name.lower() != wanted:
            continue
        header_value = header.get("value")
        return header_value if isinstance(header_value, str) else None
    return None


def _decode_body(data: str) -> str | None:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")
    except Exception:
        return None


def _bodies(payload: dict) -> tuple[str | None, str | None]:
    """First text/plain part, and first text/html part. Attachments are skipped."""
    plain: str | None = None
    html: str | None = None

    def visit(part: object) -> None:
        nonlocal plain, html
        if not isinstance(part, dict):
            return
        filename = part.get("filename")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = body.get("data") if isinstance(body.get("data"), str) else None
        if data and not filename:
            text = _decode_body(data)
            mime = part.get("mimeType")
            if mime == "text/plain" and plain is None:
                plain = text
            elif mime == "text/html" and html is None:
                html = text
        children = part.get("parts")
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(payload)
    return plain, html


def _received_at(item: dict) -> datetime | None:
    raw = item.get("internalDate")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        millis = raw
    elif isinstance(raw, str) and raw.isdigit():
        millis = int(raw)
    else:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def gmail_message_fields(item: dict) -> dict | None:
    """Map one Gmail message to stored columns. HTML never lands in body_text."""
    gmail_id = item.get("id")
    if not isinstance(gmail_id, str) or not gmail_id:
        return None
    received_at = _received_at(item)
    if received_at is None:
        return None
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    labels = item.get("labelIds")
    if isinstance(labels, list):
        is_read: bool | None = "UNREAD" not in labels
    else:
        is_read = None
    from_name, from_email = _sender(_header(payload, "From"))
    plain, html = _bodies(payload)
    thread_id = item.get("threadId")
    labels = item.get("labelIds") if isinstance(item.get("labelIds"), list) else []
    direction = "outgoing" if "SENT" in labels and "INBOX" not in labels else "incoming"
    return {
        "graph_id": gmail_id[:512],
        "subject": to_plain_text(_header(payload, "Subject")),
        "body_preview": to_plain_text(item.get("snippet")),
        "received_at": received_at,
        "from_name": from_name,
        "from_email": from_email,
        "is_read": is_read,
        "body_text": to_plain_text(plain) or to_plain_text(html),
        "body_html": sanitize_body_html(html),
        "conversation_id": thread_id[:512] if isinstance(thread_id, str) and thread_id else None,
        "to_recipients": header_people(_header(payload, "To")),
        "cc_recipients": header_people(_header(payload, "Cc")),
        "reply_to": header_people(_header(payload, "Reply-To")),
        "direction": direction,
        "origin": "provider",
    }


def _get(url: str, access_token: str) -> dict:
    try:
        payload = google_get(url, access_token)
    except HTTPError as exc:
        exc.read()
        raise GmailRequestError(exc.code) from None
    if not isinstance(payload, dict):
        raise GmailRequestError(0)
    return payload


def collect_gmail_messages(access_token: str) -> list[dict]:
    """List recent INBOX ids, then GET each message with format=full.

    Pages stay on the Gmail host. At most four pages of 25. A missing message
    is skipped. Any other HTTP error stops the sync.
    """
    page_token: str | None = None
    pages = 0
    items: list[dict] = []
    while pages < 4:
        params = {"labelIds": "INBOX", "maxResults": "25"}
        if page_token:
            params["pageToken"] = page_token
        payload = _get(f"{_LIST_BASE}?{urlencode(params)}", access_token)
        refs = payload.get("messages")
        if isinstance(refs, list):
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                message_id = ref.get("id")
                if not isinstance(message_id, str):
                    continue
                try:
                    url = gmail_message_url(message_id)
                except ValueError:
                    continue
                try:
                    items.append(_get(url, access_token))
                except GmailRequestError as exc:
                    if exc.status == 404:
                        continue
                    raise
        pages += 1
        nxt = payload.get("nextPageToken")
        if not isinstance(nxt, str) or not nxt:
            break
        page_token = nxt
    return items
