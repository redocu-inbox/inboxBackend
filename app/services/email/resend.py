"""Resend HTTPS API. Response bodies are discarded so a token cannot land in a log."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.services.email.base import EmailDeliveryError, EmailMessage

_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10


class ResendEmailSender:
    def __init__(self, api_key: str, sender: str) -> None:
        self._api_key = api_key
        self._sender = sender

    def send(self, message: EmailMessage) -> None:
        payload = json.dumps(
            {
                "from": self._sender,
                "to": [message.to],
                "subject": message.subject,
                "text": message.text,
            }
        ).encode()
        request = Request(
            _ENDPOINT,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
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
            raise EmailDeliveryError(f"status={exc.code}") from None
        except URLError:
            raise EmailDeliveryError("status=timeout") from None
