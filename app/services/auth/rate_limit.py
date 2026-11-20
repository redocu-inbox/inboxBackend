"""Fixed-window counters for account authentication flows."""

import hashlib
import ipaddress

import redis
from fastapi import Request

from app.config import get_settings

_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


class RateLimitUnavailable(Exception):
    """The counter store could not be used. Callers must not continue into lookup."""


class RateLimitExceeded(Exception):
    """An attempt bucket is over the window limit."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def client_ip(request: Request) -> str:
    """Return the peer address, or a proxy-appended address when hops are configured.

    X-Forwarded-For is ignored unless FORWARDED_FOR_HOPS is at least 1. The
    chosen address is that many entries from the right, which is the one a
    trusted proxy appended, not the left-most value a client can forge.
    """
    settings = get_settings()
    peer = request.client.host if request.client is not None else ""
    hops = settings.forwarded_for_hops
    if hops <= 0:
        return _valid_ip(peer)
    header = request.headers.get("x-forwarded-for", "")
    parts = [part.strip() for part in header.split(",") if part.strip()]
    if len(parts) < hops:
        return _valid_ip(peer)
    return _valid_ip(parts[-hops])


def _valid_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return "invalid"


def _client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def enforce_password_reset_limit(*, email: str, ip: str) -> None:
    """Increment the email and IP windows. Raise before any account lookup."""
    settings = get_settings()
    keys = (
        f"pwdreset:email:{_digest(email)}",
        f"pwdreset:ip:{_digest(ip)}",
    )
    _enforce_limit(keys, settings.password_reset_limit, settings.password_reset_window_seconds)


def enforce_account_deletion_limit(user_id: str) -> None:
    """Limit password checks for an authenticated account independently of password reset."""
    _enforce_limit((f"account-delete:user:{_digest(user_id)}",), limit=5, window_seconds=15 * 60)


def _enforce_limit(keys: tuple[str, ...], limit: int, window_seconds: int) -> None:
    try:
        client = _client()
        try:
            counts = [int(client.eval(_SCRIPT, 1, key, str(window_seconds))) for key in keys]
        finally:
            client.close()
    except Exception:
        raise RateLimitUnavailable() from None
    if any(count > limit for count in counts):
        raise RateLimitExceeded()
