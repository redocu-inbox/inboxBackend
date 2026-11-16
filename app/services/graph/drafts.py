"""Create or update an unsent Outlook reply draft.

This module never calls `/send`. A failure raises `GraphDraftError` with a
short code. The token, the draft text, and attachment bytes are not logged.
"""

import base64
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_GRAPH_ORIGIN = "https://graph.microsoft.com"
_TIMEOUT_SECONDS = 30


class GraphDraftError(Exception):
    """Graph did not save the draft. The message is a short code, not a response body."""


def create_reply_draft(access_token: str, graph_message_id: str) -> str:
    """POST createReply and return the new draft id. Does not send the message."""
    payload = _graph_request(
        "POST",
        _message_url(graph_message_id, "createReply"),
        access_token,
        {},
    )
    draft_id = payload.get("id")
    if not isinstance(draft_id, str) or not draft_id:
        raise GraphDraftError("missing_id")
    return draft_id


def patch_reply_draft(access_token: str, draft_id: str, body: str) -> None:
    """PATCH the draft body as plain text. Does not send the message."""
    _graph_request(
        "PATCH",
        _message_url(draft_id, None),
        access_token,
        {"body": {"contentType": "Text", "content": body}},
    )


def add_file_attachment(access_token: str, draft_id: str, name: str, content: bytes) -> None:
    """POST a fileAttachment onto an unsent draft. Does not send the message."""
    _graph_request(
        "POST",
        _message_url(draft_id, "attachments"),
        access_token,
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentBytes": base64.b64encode(content).decode("ascii"),
        },
    )


def _message_url(message_id: str, action: str | None) -> str:
    if action not in {None, "createReply", "attachments"}:
        raise GraphDraftError("refused")
    if not isinstance(message_id, str) or not message_id:
        raise GraphDraftError("invalid_message")
    quoted = quote(message_id, safe="")
    path = f"/v1.0/me/messages/{quoted}"
    if action is not None:
        path = f"{path}/{action}"
    url = f"{_GRAPH_ORIGIN}{path}"
    if "/send" in url.lower():
        raise GraphDraftError("refused")
    return url


def _graph_request(method: str, url: str, access_token: str, payload: dict) -> dict:
    """One Graph write. The token is a header. The payload is not logged."""
    if method not in {"POST", "PATCH"} or not url.startswith(f"{_GRAPH_ORIGIN}/"):
        raise GraphDraftError("refused")
    if "/send" in url.lower():
        raise GraphDraftError("refused")
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        exc.read()
        logger.warning("outlook draft write failed status=%s", exc.code)
        raise GraphDraftError("graph_unavailable") from None
    except (URLError, TimeoutError) as exc:
        logger.warning("outlook draft write failed error_type=%s", type(exc).__name__)
        raise GraphDraftError("graph_unavailable") from None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("outlook draft write failed error_type=JSONDecodeError")
        raise GraphDraftError("graph_unavailable") from None
    if not isinstance(parsed, dict):
        raise GraphDraftError("graph_unavailable")
    return parsed
