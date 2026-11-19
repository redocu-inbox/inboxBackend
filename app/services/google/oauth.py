"""Google OAuth token exchange via urllib. No SDK."""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import get_settings
from app.services.graph.tokens import TokenRefreshError

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
_GOOGLE_SCOPE = f"openid email {GMAIL_MODIFY_SCOPE}"
_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class GoogleTokenUnavailable(Exception):
    """The token endpoint failed and the stored grant should be kept.

    Network errors and HTTP 5xx land here. The sync records gmail_unavailable
    and the user is not asked to reconnect.
    """


def _oauth_error_code(body: bytes) -> str | None:
    """The OAuth `error` code, if the body is a JSON object. Descriptions are dropped."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) else None


def _google_token_post(fields: dict[str, str]) -> dict:
    """POST form fields to the fixed token endpoint. Response bodies are not logged.

    `invalid_grant` raises TokenRefreshError. Everything else raises
    GoogleTokenUnavailable, including network errors and HTTP 5xx, so a Google
    outage is not stored as a revoked grant.
    """
    request = Request(
        _TOKEN_URL,
        data=urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        error = _oauth_error_code(exc.read())
        logger.warning("Google token request failed status=%s error=%s", exc.code, error)
        # A 5xx is an outage even when the body says invalid_grant.
        # Only a 4xx invalid_grant means the grant itself is dead.
        if exc.code < 500 and error == "invalid_grant":
            raise TokenRefreshError("invalid_grant") from None
        raise GoogleTokenUnavailable() from None
    except (URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Google token request failed error_type=%s", type(exc).__name__)
        raise GoogleTokenUnavailable() from None
    if not isinstance(payload, dict):
        raise GoogleTokenUnavailable()
    error = payload.get("error")
    if error == "invalid_grant":
        logger.warning("Google token request rejected error=%s", error)
        raise TokenRefreshError("invalid_grant")
    if error or "access_token" not in payload:
        logger.warning(
            "Google token request rejected error=%s",
            error if isinstance(error, str) else "missing_access_token",
        )
        raise GoogleTokenUnavailable()
    return payload


def exchange_authorization_code(code: str) -> dict | None:
    """Exchange an authorization code for tokens. Mirrors MSAL's code exchange shape.

    Failures return None. The callback redirects to token_exchange and does not
    treat a Google outage during connect as a revoked mailbox.
    """
    settings = get_settings()
    try:
        return _google_token_post(
            {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.google_redirect_uri,
            }
        )
    except (TokenRefreshError, GoogleTokenUnavailable):
        return None


def exchange_refresh_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token. Google may omit a new refresh token.

    Raises TokenRefreshError for invalid_grant. Raises GoogleTokenUnavailable
    for a timeout, a network error, or any other rejection.
    """
    settings = get_settings()
    return _google_token_post(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )


def revoke_google_token(token: str) -> None:
    """Ask Google to revoke a refresh token. The token is not logged.

    A network or HTTP failure is logged as a status only. The caller still
    deletes the local copy so this API cannot keep using the mailbox.
    """
    request = Request(
        _REVOKE_URL,
        data=urlencode({"token": token}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except HTTPError as exc:
        exc.read()
        logger.warning("Google token revoke failed status=%s", exc.code)
    except (URLError, OSError) as exc:
        logger.warning("Google token revoke failed error_type=%s", type(exc).__name__)


def google_authorization_url(state: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": _GOOGLE_SCOPE,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{_AUTHORIZATION_URL}?{query}"
