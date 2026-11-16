"""Create and renew an Outlook inbox subscription.

This module does not send mail. A failure raises `GraphSubscriptionError`
with the HTTP status. The token and the client state are not logged.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.models.user import utcnow

logger = logging.getLogger(__name__)

_GRAPH_ORIGIN = "https://graph.microsoft.com"
_TIMEOUT_SECONDS = 30
_EXPIRY_MINUTES = 4000
_RESOURCE = "me/mailFolders('Inbox')/messages"
_MAX_RETRY_WAIT_SECONDS = 60


class GraphSubscriptionError(Exception):
    """Graph did not save the subscription. The message is a short code."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.retry_after = retry_after


def create_graph_subscription(
    access_token: str,
    *,
    notification_url: str,
    client_state: str,
) -> tuple[str, datetime]:
    """POST a subscription and return its id and expiry. Does not send mail."""
    expires = utcnow() + timedelta(minutes=_EXPIRY_MINUTES)
    payload = _graph_write(
        "POST",
        f"{_GRAPH_ORIGIN}/v1.0/subscriptions",
        access_token,
        {
            "changeType": "created,updated",
            "notificationUrl": notification_url,
            "resource": _RESOURCE,
            "expirationDateTime": _iso(expires),
            "clientState": client_state,
        },
    )
    subscription_id = payload.get("id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise GraphSubscriptionError("missing_id")
    return subscription_id, _parse_expiry(payload.get("expirationDateTime"), expires)


def renew_graph_subscription(access_token: str, subscription_id: str) -> datetime:
    """PATCH the expiry. A missing subscription is status 404."""
    if not isinstance(subscription_id, str) or not subscription_id:
        raise GraphSubscriptionError("refused")
    expires = utcnow() + timedelta(minutes=_EXPIRY_MINUTES)
    payload = _graph_write(
        "PATCH",
        f"{_GRAPH_ORIGIN}/v1.0/subscriptions/{quote(subscription_id, safe='')}",
        access_token,
        {"expirationDateTime": _iso(expires)},
    )
    return _parse_expiry(payload.get("expirationDateTime"), expires)


def retry_after_seconds(value: str | None) -> int:
    """Seconds to wait once. A missing or non-numeric header waits zero. Capped."""
    if not value:
        return 0
    try:
        seconds = int(value)
    except ValueError:
        return 0
    if seconds < 0:
        return 0
    return min(seconds, _MAX_RETRY_WAIT_SECONDS)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_expiry(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _graph_write(method: str, url: str, access_token: str, payload: dict) -> dict:
    """One Graph write. The token is a header. The payload is not logged."""
    if method not in {"POST", "PATCH"} or not url.startswith(f"{_GRAPH_ORIGIN}/"):
        raise GraphSubscriptionError("refused")
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
        retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
        logger.warning("graph subscription write failed status=%s", exc.code)
        raise GraphSubscriptionError(
            "graph_unavailable",
            status=exc.code,
            retry_after=retry_after if isinstance(retry_after, str) else None,
        ) from None
    except (URLError, TimeoutError) as exc:
        logger.warning("graph subscription write failed error_type=%s", type(exc).__name__)
        raise GraphSubscriptionError("graph_unavailable") from None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("graph subscription write failed error_type=JSONDecodeError")
        raise GraphSubscriptionError("graph_unavailable") from None
    if not isinstance(parsed, dict):
        raise GraphSubscriptionError("graph_unavailable")
    return parsed
