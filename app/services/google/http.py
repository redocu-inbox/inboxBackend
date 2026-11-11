"""Bearer GETs to Gmail and Google user APIs. Host allowlist only applies here, not the token URL."""

import json
import logging
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_GOOGLE_HOSTS = ("https://gmail.googleapis.com/", "https://www.googleapis.com/")
_TIMEOUT_SECONDS = 30

USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GMAIL_INBOX_LIST_URL = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    "?labelIds=INBOX&maxResults=25"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoogleMailboxIdentity:
    """Stable Google account id and mailbox address from userinfo (not Gmail profile)."""

    provider_account_id: str
    email: str


def google_get(url: str, access_token: str) -> dict:
    """GET one Google API URL. The token is a header and must not be logged."""
    if not url.startswith(_GOOGLE_HOSTS):
        raise ValueError("unexpected Google host")
    request = Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return json.load(response)


def fetch_mailbox_identity(access_token: str) -> GoogleMailboxIdentity | None:
    """Load `sub` and `email` after token exchange. Replaces MSAL id_token_claims for Google."""
    try:
        payload = google_get(USERINFO_URL, access_token)
    except HTTPError as exc:
        exc.read()
        logger.warning("Google userinfo failed status=%s", exc.code)
        return None
    except Exception as exc:
        logger.warning("Google userinfo failed error_type=%s", type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    provider_account_id = payload.get("sub")
    email = payload.get("email")
    if (
        not isinstance(provider_account_id, str)
        or not provider_account_id
        or not isinstance(email, str)
        or not email
    ):
        return None
    return GoogleMailboxIdentity(provider_account_id=provider_account_id, email=email)


def gmail_message_url(message_id: str) -> str:
    """Full-format message GET URL. Pass the result to `google_get`, not users.getProfile."""
    if not isinstance(message_id, str) or not message_id or "/" in message_id:
        raise ValueError("invalid gmail message id")
    return (
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
        "?format=full"
    )
