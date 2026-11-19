"""Prepare, submit, and confirm approved mail using the original mailbox."""

import base64
import json
import logging
from collections import Counter
from email.message import EmailMessage
from http.client import HTTPException as HTTPConnectionError
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_GRAPH_ORIGIN = "https://graph.microsoft.com"
_GRAPH_MESSAGES = f"{_GRAPH_ORIGIN}/v1.0/me/messages"
_GMAIL_MESSAGES = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_IMMUTABLE_ID = 'IdType="ImmutableId"'
_TIMEOUT_SECONDS = 30
_SEND_REJECTED = {400, 401, 403, 413, 415, 422, 429}


class MailSendError(Exception):
    """The request was rejected, or preparation failed before sending."""


class MailSendUncertain(MailSendError):
    """The send request may have been accepted before the connection failed."""


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    *,
    immutable_id: bool = False,
    sending: bool = False,
) -> dict:
    if not url.startswith((f"{_GRAPH_ORIGIN}/", f"{_GMAIL_MESSAGES}")):
        raise MailSendError("unexpected_provider_url")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if immutable_id:
        headers["Prefer"] = _IMMUTABLE_ID
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            if sending:
                return {}
            raw = response.read()
    except HTTPError as exc:
        exc.close()
        logger.warning("mail provider request failed status=%s", exc.code)
        if sending and exc.code not in _SEND_REJECTED:
            raise MailSendUncertain("send_unconfirmed") from None
        if method == "GET" and exc.code == 404:
            return {}
        raise MailSendError("provider_rejected_request") from None
    except (OSError, HTTPConnectionError) as exc:
        logger.warning("mail provider request failed error_type=%s", type(exc).__name__)
        if sending:
            raise MailSendUncertain("send_unconfirmed") from None
        raise MailSendError("provider_unavailable") from None

    try:
        result = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise MailSendError("invalid_provider_response") from None
    if not isinstance(result, dict):
        raise MailSendError("invalid_provider_response")
    return result


def _graph_people(recipients: list[str]) -> list[dict]:
    people = []
    for recipient in recipients:
        name, separator, email = recipient.rpartition(" <")
        address = {"address": recipient}
        if separator and email.endswith(">"):
            address = {"address": email[:-1].strip()}
            name = name.strip().strip('"')
            if name:
                address["name"] = name
        people.append({"emailAddress": address})
    return people


def _graph_files(files: list[tuple[str, bytes, str | None]]) -> list[dict]:
    return [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": content_type or "application/octet-stream",
            "contentBytes": base64.b64encode(content).decode("ascii"),
        }
        for name, content, content_type in files
    ]


def _message_url(message_id: str) -> str:
    return f"{_GRAPH_MESSAGES}/{quote(message_id, safe='')}"


def _immutable_draft_id(token: str, draft_id: str) -> str:
    result = _request(
        "POST", f"{_GRAPH_ORIGIN}/v1.0/me/translateExchangeIds", token,
        {"inputIds": [draft_id], "sourceIdType": "restId", "targetIdType": "restImmutableEntryId"},
    )
    entries = result.get("value")
    entry = entries[0] if isinstance(entries, list) and entries else None
    target_id = entry.get("targetId") if isinstance(entry, dict) else None
    if not isinstance(target_id, str) or not target_id:
        raise MailSendError("missing_immutable_draft_id")
    return target_id


def _attach_outlook_files(token: str, draft_id: str, attachments: list[dict]) -> None:
    if not attachments:
        return
    url = f"{_message_url(draft_id)}/attachments"
    existing = Counter()
    next_url = url
    while next_url:
        page = _request("GET", next_url, token, immutable_id=True)
        for item in page.get("value", []):
            if isinstance(item, dict) and item.get("@odata.type") == "#microsoft.graph.fileAttachment":
                existing[(item.get("name"), item.get("contentBytes"))] += 1
        next_url = page.get("@odata.nextLink")
    for attachment in attachments:
        key = (attachment["name"], attachment["contentBytes"])
        if existing[key]:
            existing[key] -= 1
        else:
            _request("POST", url, token, attachment, immutable_id=True)


def prepare_outlook_draft(
    token: str,
    *,
    original_id: str | None,
    saved_draft_id: str | None,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
) -> str:
    """Save the approved snapshot to an Outlook draft and return its immutable ID."""
    message = {
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _graph_people(to),
        "ccRecipients": _graph_people(cc),
        "bccRecipients": [],
    }
    attachments = _graph_files(files)
    if saved_draft_id:
        draft_id = _immutable_draft_id(token, saved_draft_id)
    else:
        if original_id:
            url = f"{_message_url(original_id)}/createReply"
            payload = None
        else:
            url = _GRAPH_MESSAGES
            payload = {**message, "subject": subject, "attachments": attachments}
        draft = _request("POST", url, token, payload, immutable_id=True)
        draft_id = draft.get("id")
        if not isinstance(draft_id, str) or not draft_id:
            raise MailSendError("missing_draft_id")
    if original_id or saved_draft_id:
        _request("PATCH", _message_url(draft_id), token, message, immutable_id=True)
        _attach_outlook_files(token, draft_id, attachments)
    return draft_id


def send_outlook_draft(token: str, draft_id: str) -> None:
    _request("POST", f"{_message_url(draft_id)}/send", token, immutable_id=True, sending=True)


def outlook_sent(token: str, draft_id: str) -> bool:
    payload = _request("GET", f"{_message_url(draft_id)}?$select=isDraft,sentDateTime", token, immutable_id=True)
    return payload.get("isDraft") is False and bool(payload.get("sentDateTime"))


def prepare_gmail_message(
    *,
    sender: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
    thread_id: str | None,
    message_id: str,
) -> dict:
    message = EmailMessage()
    try:
        message["From"] = sender
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message["Message-ID"] = f"<{message_id}>"
        message.set_content(body)
        for name, content, content_type in files:
            maintype, _, subtype = (content_type or "").partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
        payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    except (ValueError, TypeError):
        raise MailSendError("invalid_message_format") from None
    if thread_id and not thread_id.startswith("local-"):
        payload["threadId"] = thread_id
    return payload


def send_gmail(token: str, payload: dict) -> None:
    _request("POST", f"{_GMAIL_MESSAGES}/send", token, payload, sending=True)


def gmail_sent(token: str, message_id: str) -> bool:
    query = urlencode({"labelIds": "SENT", "q": f"rfc822msgid:{message_id}", "maxResults": 1})
    payload = _request("GET", f"{_GMAIL_MESSAGES}?{query}", token)
    return bool(payload.get("messages"))
