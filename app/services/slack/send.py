"""Post a Slack message as the connected user.

Only the authenticated send routes call this module. Sync, scoring, drafts,
and approve do not.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.config import get_settings
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.slack import SlackSentMessage
from app.models.user import User, utcnow
from app.services.mailboxes import lock_active_mailbox
from app.services.priority.apply import classify_and_score
from app.services.slack.http import SlackApiError, SlackRateLimited, retry_after_seconds, slack_api

_CHANNEL = re.compile(r"^[CDG][A-Z0-9]{2,31}$")
_TS = re.compile(r"^[0-9]{1,16}\.[0-9]{1,6}$")
_TEXT_LIMIT = 40_000

_SEND_OFF = "Slack sending is turned off."
_RECONNECT = "Reconnect Slack to allow sending."
_NOT_IN_CHANNEL = "You are not in that Slack conversation."
_CHANNEL_MISSING = "Slack conversation not found."
_RATE = "Slack is rate limiting requests. Try again shortly."
_FAILED = "Slack could not send this message."
_EMPTY = "Enter a message."
_TOO_LONG = "Slack messages must be 40000 characters or fewer."


def chat_write_granted(scopes: str | None) -> bool:
    if not scopes:
        return False
    parts = {item.strip() for item in scopes.replace(",", " ").split() if item.strip()}
    return "chat:write" in parts


def send_slack_reply(session: Session, user: User, message_id, text: str) -> Message:
    """Reply in the stored message's thread. Another user's row is a 404."""
    message = session.get(Message, message_id)
    if message is None or message.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    account = session.get(ConnectedAccount, message.connected_account_id)
    if (
        account is None
        or account.user_id != user.id
        or account.provider != "slack"
        or account.disconnected_at is not None
        or not message.slack_channel_id
        or not message.slack_thread_ts
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
    return _post(
        session,
        user,
        account,
        channel=message.slack_channel_id,
        thread_ts=message.slack_thread_ts,
        text=text,
        conversation_id=message.conversation_id or message.slack_thread_ts,
    )


def send_slack_new(
    session: Session,
    user: User,
    *,
    channel: str,
    text: str,
    thread_ts: str | None,
) -> Message:
    """Send to a conversation id. The account is always the caller's active Slack connection."""
    if not isinstance(channel, str) or not _CHANNEL.fullmatch(channel):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown Slack conversation.")
    if thread_ts is not None and (not isinstance(thread_ts, str) or not _TS.fullmatch(thread_ts)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown Slack thread.")
    account = lock_active_mailbox(session, user.id, provider="slack")
    if account is None:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No mailbox is connected.")
    return _post(
        session,
        user,
        account,
        channel=channel,
        thread_ts=thread_ts,
        text=text,
        conversation_id=thread_ts or channel,
    )


def _post(
    session: Session,
    user: User,
    account: ConnectedAccount,
    *,
    channel: str,
    thread_ts: str | None,
    text: str,
    conversation_id: str,
) -> Message:
    if not isinstance(text, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_EMPTY)
    cleaned = text.strip()
    if not cleaned:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_EMPTY)
    if len(cleaned) > _TEXT_LIMIT:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_TOO_LONG)

    token = account.access_token or ""
    scopes = account.scopes
    account_id = account.id
    label = account.email
    log = SlackSentMessage(
        user_id=user.id,
        connected_account_id=account_id,
        channel_id=channel,
        thread_ts=thread_ts,
        text=cleaned,
        status="pending",
        created_at=utcnow(),
    )
    session.add(log)
    session.commit()

    if not get_settings().slack_send_enabled:
        _finish_log(session, log, status="failed", error="send_disabled")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_SEND_OFF)
    if not chat_write_granted(scopes):
        _finish_log(session, log, status="failed", error="missing_scope")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_RECONNECT)
    if not token:
        _finish_log(session, log, status="failed", error="no_token")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_RECONNECT)

    body: dict[str, str] = {"channel": channel, "text": cleaned}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        payload = slack_api(token, "chat.postMessage", body=body)
    except SlackRateLimited as exc:
        wait = retry_after_seconds(exc.retry_after)
        _finish_log(session, log, status="failed", error="ratelimited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_RATE,
            headers={"Retry-After": str(wait)},
        ) from None
    except SlackApiError as exc:
        _finish_log(session, log, status="failed", error=exc.code[:64])
        raise _send_http(exc) from None

    slack_ts = _posted_ts(payload)
    if slack_ts is None:
        _finish_log(session, log, status="failed", error="missing_ts")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_FAILED)
    _finish_log(session, log, status="sent", error=None, slack_ts=slack_ts)
    return _store_sent(
        session,
        user_id=user.id,
        account_id=account_id,
        label=label,
        channel=channel,
        thread_ts=thread_ts or slack_ts,
        conversation_id=conversation_id,
        text=cleaned,
        slack_ts=slack_ts,
    )


def _send_http(exc: SlackApiError) -> HTTPException:
    if exc.code == "missing_scope":
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_RECONNECT)
    if exc.code == "not_in_channel":
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_NOT_IN_CHANNEL)
    if exc.code == "channel_not_found":
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_CHANNEL_MISSING)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_FAILED)


def _finish_log(
    session: Session,
    log: SlackSentMessage,
    *,
    status: str,
    error: str | None,
    slack_ts: str | None = None,
) -> None:
    current = session.get(SlackSentMessage, log.id)
    if current is None:
        return
    current.status = status
    current.error = error
    if slack_ts is not None:
        current.slack_ts = slack_ts[:32]
    session.add(current)
    session.commit()


def _posted_ts(payload: dict) -> str | None:
    ts = payload.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    message = payload.get("message")
    if isinstance(message, dict):
        nested = message.get("ts")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _store_sent(
    session: Session,
    *,
    user_id,
    account_id,
    label: str,
    channel: str,
    thread_ts: str,
    conversation_id: str,
    text: str,
    slack_ts: str,
) -> Message:
    received = _received_at(slack_ts) or utcnow()
    graph_id = f"{channel}:{slack_ts}"[:512]
    existing = session.exec(
        select(Message).where(
            Message.connected_account_id == account_id,
            Message.graph_id == graph_id,
        )
    ).first()
    fields = {
        "subject": None,
        "body_preview": text[:280] or None,
        "body_text": text,
        "from_name": label,
        "from_email": None,
        "is_read": True,
        "received_at": received,
        "conversation_id": conversation_id[:512],
        "direction": "outgoing",
        "origin": "provider",
        "slack_channel_id": channel,
        "slack_thread_ts": thread_ts[:32],
        "slack_permalink": _permalink(channel, slack_ts, thread_ts),
    }
    if existing is None:
        existing = Message(
            user_id=user_id,
            connected_account_id=account_id,
            graph_id=graph_id,
            **fields,
        )
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
        existing.updated_at = utcnow()
    classify_and_score(existing, provider="slack")
    session.add(existing)
    session.commit()
    session.refresh(existing)
    return existing


def _permalink(channel: str, ts: str, thread_ts: str | None) -> str:
    url = f"https://app.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        return f"{url}?thread_ts={thread_ts}"
    return url


def _received_at(ts: str) -> datetime | None:
    try:
        value = Decimal(ts)
    except InvalidOperation:
        return None
    seconds = int(value)
    micros = min(999_999, max(0, int((value - Decimal(seconds)) * Decimal(1_000_000))))
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=micros)
    except (OverflowError, OSError, ValueError):
        return None
