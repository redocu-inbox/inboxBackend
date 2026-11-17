"""Send one approved snapshot through Outlook or Gmail.

This module is the only place that calls a provider send URL. The unsent
Outlook draft helper stays unable to send. Tokens and message bodies are not
logged.
"""

import base64
import json
import logging
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_GRAPH_ORIGIN = "https://graph.microsoft.com"
_GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TIMEOUT_SECONDS = 30


class MailSendError(Exception):
    """The provider did not accept the message. The draft stays unsent."""


def _split(recipient: str) -> tuple[str | None, str]:
    text = recipient.strip()
    if text.endswith(">") and " <" in text:
        name, email = text[:-1].rsplit(" <", 1)
        if name.startswith('"') and name.endswith('"') and len(name) >= 2:
            name = name[1:-1]
        return (name or None), email.strip()
    return None, text


def _graph_people(recipients: list[str]) -> list[dict]:
    people = []
    for recipient in recipients:
        name, email = _split(recipient)
        address = {"address": email}
        if name:
            address["name"] = name
        people.append({"emailAddress": address})
    return people


def _graph_files(files: list[tuple[str, bytes, str | None]]) -> list[dict]:
    attachments = []
    for name, content, _content_type in files:
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentBytes": base64.b64encode(content).decode("ascii"),
            }
        )
    return attachments


def _graph_post(url: str, access_token: str, payload: dict) -> None:
    if not url.startswith(f"{_GRAPH_ORIGIN}/") or access_token in url:
        raise MailSendError("refused")
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except HTTPError as exc:
        exc.read()
        logger.warning("outlook send failed status=%s", exc.code)
        raise MailSendError("graph_unavailable") from None
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("outlook send failed error_type=%s", type(exc).__name__)
        raise MailSendError("graph_unavailable") from None


def send_outlook_reply(
    access_token: str,
    graph_message_id: str,
    *,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
) -> None:
    """POST reply for one received message. The saved body and recipients are the message."""
    if not graph_message_id or graph_message_id.startswith("local-"):
        raise MailSendError("not_a_reply")
    quoted = quote(graph_message_id, safe="")
    message: dict = {
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _graph_people(to),
    }
    if cc:
        message["ccRecipients"] = _graph_people(cc)
    if files:
        message["attachments"] = _graph_files(files)
    _graph_post(f"{_GRAPH_ORIGIN}/v1.0/me/messages/{quoted}/reply", access_token, {"message": message})


def send_outlook_new(
    access_token: str,
    *,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
) -> None:
    """POST sendMail for a new email or a forward. The subject is sent as stored."""
    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _graph_people(to),
    }
    if cc:
        message["ccRecipients"] = _graph_people(cc)
    if files:
        message["attachments"] = _graph_files(files)
    _graph_post(
        f"{_GRAPH_ORIGIN}/v1.0/me/sendMail",
        access_token,
        {"message": message, "saveToSentItems": True},
    )


def _gmail_raw(
    sender: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
) -> str:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message.set_content(body)
    for name, content, content_type in files:
        media = content_type or "application/octet-stream"
        if "/" not in media:
            media = "application/octet-stream"
        maintype, subtype = media.split("/", 1)
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send_gmail(
    access_token: str,
    *,
    sender: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str],
    files: list[tuple[str, bytes, str | None]],
    thread_id: str | None,
) -> None:
    """POST users.messages.send. A thread id keeps a reply on the same Gmail thread."""
    payload: dict = {"raw": _gmail_raw(sender, subject, body, to, cc, files)}
    if thread_id and not thread_id.startswith("local-"):
        payload["threadId"] = thread_id
    request = Request(
        _GMAIL_SEND,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except HTTPError as exc:
        exc.read()
        logger.warning("gmail send failed status=%s", exc.code)
        raise MailSendError("gmail_unavailable") from None
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("gmail send failed error_type=%s", type(exc).__name__)
        raise MailSendError("gmail_unavailable") from None
