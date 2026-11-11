"""Google OAuth token exchange via urllib. No SDK."""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import get_settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_SCOPE = "openid email https://www.googleapis.com/auth/gmail.readonly"
_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


def _google_token_post(fields: dict[str, str]) -> dict | None:
    """POST form fields to the fixed token endpoint. Response bodies are not logged."""
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
        exc.read()
        logger.warning("Google token request failed status=%s", exc.code)
        return None
    except (URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Google token request failed error_type=%s", type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error") or "access_token" not in payload:
        logger.warning("Google token request rejected error=%s", payload.get("error"))
        return None
    return payload


def exchange_authorization_code(code: str) -> dict | None:
    """Exchange an authorization code for tokens. Mirrors MSAL's code exchange shape."""
    settings = get_settings()
    return _google_token_post(
        {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.google_redirect_uri,
        }
    )


def exchange_refresh_token(refresh_token: str) -> dict | None:
    """Exchange a refresh token for a new access token. Google may omit a new refresh token."""
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