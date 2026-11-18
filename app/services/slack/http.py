"""Slack Web API. The token is a header and is never logged.

Sync and send both call `slack_api`. A 429 is retried at most three times.
`Retry-After` is capped so one response cannot stall the worker.
"""

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_API = "https://slack.com/api/"
_TIMEOUT_SECONDS = 30
_MAX_RETRIES = 3
_MAX_SLEEP_SECONDS = 30

logger = logging.getLogger(__name__)


class SlackApiError(Exception):
    """Slack rejected the call or the network failed. `code` is a short token."""

    def __init__(self, code: str, *, status: int = 0, retry_after: int | None = None) -> None:
        self.code = code
        self.status = status
        self.retry_after = retry_after
        super().__init__(code)


class SlackRateLimited(SlackApiError):
    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("ratelimited", status=429, retry_after=retry_after)


def retry_after_seconds(value: object) -> int:
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        seconds = 1
    if seconds < 0:
        seconds = 0
    return min(seconds, _MAX_SLEEP_SECONDS)


def slack_api(
    token: str,
    method: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Call one Slack method. GET when `body` is omitted, otherwise POST JSON."""
    if not _method_ok(method):
        raise SlackApiError("invalid_method")
    last_wait: int | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return _once(token, method, params=params, body=body)
        except SlackRateLimited as exc:
            last_wait = retry_after_seconds(exc.retry_after)
            if attempt >= _MAX_RETRIES:
                raise SlackRateLimited(last_wait) from None
            time.sleep(last_wait)
    raise SlackRateLimited(last_wait)


def _method_ok(method: str) -> bool:
    if not method or len(method) > 64:
        return False
    return all(part.isidentifier() for part in method.split("."))


def _once(token: str, method: str, *, params: dict | None, body: dict | None) -> dict:
    url = _API + method
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is None:
        if params:
            url = f"{url}?{_query(params)}"
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body).encode()
    request = Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
            retry_after = response.headers.get("Retry-After") if response.headers else None
    except HTTPError as exc:
        exc.read()
        wait = exc.headers.get("Retry-After") if exc.headers else None
        logger.warning("Slack API failed method=%s status=%s", method, exc.code)
        if exc.code == 429:
            raise SlackRateLimited(retry_after_seconds(wait)) from None
        raise SlackApiError("unavailable", status=exc.code) from None
    except (URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Slack API failed method=%s error_type=%s", method, type(exc).__name__)
        raise SlackApiError("unavailable") from None
    if not isinstance(payload, dict):
        raise SlackApiError("unavailable")
    if payload.get("ok") is True:
        return payload
    error = payload.get("error")
    code = error if isinstance(error, str) and error else "not_ok"
    logger.warning("Slack API rejected method=%s error=%s", method, code)
    if code == "ratelimited":
        raise SlackRateLimited(retry_after_seconds(retry_after))
    raise SlackApiError(code)


def _query(params: dict) -> str:
    clean: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        clean[str(key)] = str(value)
    return urlencode(clean)
