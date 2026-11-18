"""Slack OAuth v2 for a user token. Bot tokens are not requested or stored."""

import json
import logging
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import get_settings
from app.services.slack.http import SlackApiError, slack_api

_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
_TIMEOUT_SECONDS = 30

# user_scope, not scope. scope would ask Slack for a bot token.
USER_SCOPES = (
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "users:read",
    "chat:write",
)

logger = logging.getLogger(__name__)


class SlackOAuthError(Exception):
    """The code exchange did not produce a user token. `reason` is a redirect code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SlackOAuthGrant:
    access_token: str
    user_id: str
    team_id: str
    team_name: str
    scopes: str


def slack_is_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.slack_client_id.strip()
        and settings.slack_client_secret.strip()
        and settings.slack_redirect_uri.strip()
    )


def slack_authorization_url(state: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.slack_client_id.strip(),
            "user_scope": ",".join(USER_SCOPES),
            "redirect_uri": settings.slack_redirect_uri.strip(),
            "state": state,
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"


def exchange_slack_code(code: str) -> SlackOAuthGrant:
    """Exchange the code. A bot token, or a missing user token, is `slack_denied`.

    Network and HTTP failures are `token_exchange`. The access token is not logged.
    """
    settings = get_settings()
    request = Request(
        _ACCESS_URL,
        data=urlencode(
            {
                "client_id": settings.slack_client_id.strip(),
                "client_secret": settings.slack_client_secret.strip(),
                "code": code,
                "redirect_uri": settings.slack_redirect_uri.strip(),
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        exc.read()
        logger.warning("Slack token exchange failed status=%s", exc.code)
        raise SlackOAuthError("token_exchange") from None
    except (URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Slack token exchange failed error_type=%s", type(exc).__name__)
        raise SlackOAuthError("token_exchange") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error") if isinstance(payload, dict) else None
        logger.warning(
            "Slack token exchange rejected error=%s",
            error if isinstance(error, str) else "not_ok",
        )
        raise SlackOAuthError("token_exchange")
    grant = _user_grant(payload)
    if grant is None:
        logger.warning("Slack token exchange returned no user token")
        raise SlackOAuthError("slack_denied")
    return grant


def _user_grant(payload: dict) -> SlackOAuthGrant | None:
    authed = payload.get("authed_user")
    team = payload.get("team")
    if not isinstance(authed, dict) or not isinstance(team, dict):
        return None
    token = authed.get("access_token")
    user_id = authed.get("id")
    team_id = team.get("id")
    team_name = team.get("name") if isinstance(team.get("name"), str) and team.get("name") else team_id
    scopes = authed.get("scope")
    if (
        authed.get("token_type") != "user"
        or not isinstance(token, str)
        or not token
        or not isinstance(user_id, str)
        or not user_id
        or not isinstance(team_id, str)
        or not team_id
        or not isinstance(team_name, str)
    ):
        return None
    scope_text = scopes if isinstance(scopes, str) else ",".join(USER_SCOPES)
    return SlackOAuthGrant(
        access_token=token,
        user_id=user_id,
        team_id=team_id,
        team_name=team_name,
        scopes=scope_text,
    )


def slack_display_name(token: str, user_id: str) -> str:
    """Best display name users.info will give. A failure keeps the user id."""
    try:
        payload = slack_api(token, "users.info", params={"user": user_id})
    except SlackApiError as exc:
        logger.warning("Slack users.info failed error=%s", exc.code)
        return user_id
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    for value in (profile.get("display_name"), user.get("real_name"), user.get("name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return user_id


def revoke_slack_token(token: str) -> None:
    """Best-effort revoke. The local token is deleted even when Slack refuses."""
    try:
        slack_api(token, "auth.revoke")
    except SlackApiError as exc:
        logger.warning("Slack token revoke failed error=%s", exc.code)


def mailbox_label(display_name: str, team_name: str) -> str:
    """Label stored in the email column. These scopes do not include an address."""
    text = f"{display_name} ({team_name})"
    cleaned = " ".join(text.replace("\x00", " ").split())
    return (cleaned or "Slack")[:320]
