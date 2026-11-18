"""Pull Slack DMs, group DMs, and personal @-mentions into message fields.

This module does not send. Posting lives in app.services.slack.send and is
reached only from the authenticated send routes.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.slack import SlackSyncCursor, SlackUserCache
from app.models.user import utcnow
from app.services.slack.http import SlackApiError, slack_api

logger = logging.getLogger(__name__)

_LIST_PAGES = 5
_HISTORY_PAGES = 4
_REPLY_PAGES = 2
_BACKFILL = timedelta(days=14)
_NAME_TTL = timedelta(hours=24)
_PREVIEW_CHARS = 280
_BODY_CHARS = 100_000
_SKIP_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "pinned_item",
        "unpinned_item",
    }
)


def collect_slack_messages(session: Session, account: ConnectedAccount) -> list[dict]:
    """Read conversations and return mapped message fields. Cursors stay uncommitted.

    The caller commits the fields and the cursor rows together. A raised
    SlackApiError, including SlackRateLimited, should roll this session back.
    """
    token = account.access_token
    if not isinstance(token, str) or not token:
        raise SlackApiError("no_token")
    user_id = _connected_user_id(account)
    names: dict[str, str] = {}
    collected: list[dict] = []
    now = utcnow()
    for channel in _list_channels(token):
        channel_id = channel.get("id")
        kind = _kind(channel)
        if not isinstance(channel_id, str) or not channel_id or len(channel_id) > 32 or kind is None:
            continue
        collected.extend(
            _sync_conversation(
                session,
                account,
                token,
                user_id,
                channel_id,
                kind,
                names,
                now,
            )
        )
    return collected


def _connected_user_id(account: ConnectedAccount) -> str:
    _team, separator, user_id = account.provider_account_id.partition(":")
    if separator != ":" or not user_id:
        raise SlackApiError("invalid_account")
    return user_id


def _list_channels(token: str) -> list[dict]:
    cursor: str | None = None
    found: list[dict] = []
    for _page in range(_LIST_PAGES):
        params: dict[str, str] = {
            "types": "im,mpim,public_channel,private_channel",
            "exclude_archived": "true",
            "limit": "100",
        }
        if cursor:
            params["cursor"] = cursor
        payload = slack_api(token, "conversations.list", params=params)
        rows = payload.get("channels")
        if isinstance(rows, list):
            found.extend(row for row in rows if isinstance(row, dict))
        cursor = _next_cursor(payload)
        if not cursor:
            break
    return found


def _kind(channel: dict) -> str | None:
    if channel.get("is_archived") is True:
        return None
    if channel.get("is_im") is True:
        return "im"
    if channel.get("is_mpim") is True:
        return "mpim"
    if channel.get("is_member") is False:
        return None
    if channel.get("is_channel") is True or channel.get("is_group") is True or channel.get("is_private") is True:
        return "channel"
    return None


def _sync_conversation(
    session: Session,
    account: ConnectedAccount,
    token: str,
    user_id: str,
    channel_id: str,
    kind: str,
    names: dict[str, str],
    now: datetime,
) -> list[dict]:
    cursor = _cursor(session, account, channel_id, kind)
    state = _state(cursor, now)
    collected: list[dict] = []
    reply_budget = _REPLY_PAGES
    high = cursor.last_ts

    if state.get("thread"):
        messages, high, reply_budget, finished, reply_cursor = _replies(
            session,
            account,
            token,
            user_id,
            channel_id,
            kind,
            state["thread"],
            state,
            names,
            reply_budget,
            high,
        )
        collected.extend(messages)
        if not finished:
            state["reply"] = reply_cursor
            _save(cursor, state, high, now)
            return collected
        _mark_done(state, state["thread"])
        state["thread"] = None
        state["reply"] = None

    pages = 0
    pending: list[str] = []
    while not state["history_done"] and pages < _HISTORY_PAGES:
        params = {
            "channel": channel_id,
            "limit": "100",
            "inclusive": "false",
            "oldest": state["oldest"],
        }
        if state.get("history"):
            params["cursor"] = state["history"]
        payload = slack_api(token, "conversations.history", params=params)
        pages += 1
        rows = payload.get("messages")
        if isinstance(rows, list):
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                mapped, root_ts, needs_thread = _consider(
                    session, account, token, user_id, channel_id, kind, raw, state, names
                )
                if mapped is not None:
                    collected.append(mapped)
                    ts = mapped["graph_id"].split(":", 1)[-1]
                    high = _ts_max(high, ts)
                if needs_thread and root_ts and root_ts not in state["done"] and root_ts not in pending:
                    pending.append(root_ts)
        nxt = _next_cursor(payload)
        if not nxt:
            state["history_done"] = True
            state["history"] = None
            break
        state["history"] = nxt

    for root_ts in pending:
        if reply_budget <= 0:
            state["thread"] = root_ts
            state["reply"] = None
            _save(cursor, state, high, now)
            return collected
        messages, high, reply_budget, finished, reply_cursor = _replies(
            session,
            account,
            token,
            user_id,
            channel_id,
            kind,
            root_ts,
            state,
            names,
            reply_budget,
            high,
        )
        collected.extend(messages)
        if not finished:
            state["thread"] = root_ts
            state["reply"] = reply_cursor
            _save(cursor, state, high, now)
            return collected
        _mark_done(state, root_ts)

    if state["history_done"] and not state.get("thread"):
        cursor.last_ts = high or state.get("skip") or str(int(now.timestamp()))
        cursor.next_cursor = None
    else:
        _save(cursor, state, high, now)
        return collected
    cursor.updated_at = now
    session.add(cursor)
    return collected


def _cursor(session: Session, account: ConnectedAccount, channel_id: str, kind: str) -> SlackSyncCursor:
    row = session.exec(
        select(SlackSyncCursor).where(
            SlackSyncCursor.connected_account_id == account.id,
            SlackSyncCursor.channel_id == channel_id,
        )
    ).first()
    if row is not None:
        row.kind = kind
        return row
    row = SlackSyncCursor(
        connected_account_id=account.id,
        channel_id=channel_id,
        kind=kind,
        updated_at=utcnow(),
    )
    session.add(row)
    return row


def _state(cursor: SlackSyncCursor, now: datetime) -> dict:
    if cursor.next_cursor:
        try:
            parsed = json.loads(cursor.next_cursor)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("oldest"), str):
            parsed.setdefault("history_done", False)
            parsed.setdefault("done", [])
            return parsed
    floor = cursor.last_ts or str(int((now - _BACKFILL).timestamp()))
    return {
        "oldest": floor,
        "skip": cursor.last_ts,
        "history": None,
        "history_done": False,
        "thread": None,
        "reply": None,
        "done": [],
    }


def _save(cursor: SlackSyncCursor, state: dict, high: str | None, now: datetime) -> None:
    cursor.last_ts = high or cursor.last_ts
    cursor.next_cursor = json.dumps(state, separators=(",", ":"))
    cursor.updated_at = now


def _mark_done(state: dict, thread_ts: str) -> None:
    done = state.setdefault("done", [])
    if thread_ts not in done:
        done.append(thread_ts)


def _consider(
    session: Session,
    account: ConnectedAccount,
    token: str,
    user_id: str,
    channel_id: str,
    kind: str,
    raw: dict,
    state: dict,
    names: dict[str, str],
) -> tuple[dict | None, str | None, bool]:
    """Return mapped fields, the thread root ts, and whether replies still need a fetch."""
    ts = raw.get("ts")
    if not isinstance(ts, str) or not ts or len(ts) > 32:
        return None, None, False
    skip = state.get("skip")
    if isinstance(skip, str) and skip and _ts_leq(ts, skip):
        return None, None, False
    subtype = raw.get("subtype")
    if isinstance(subtype, str) and subtype in _SKIP_SUBTYPES:
        return None, None, False
    root = raw.get("thread_ts") if isinstance(raw.get("thread_ts"), str) else ts
    if len(root) > 32:
        root = ts
    needs_thread = _is_root(raw) and _replies_newer(raw, skip if isinstance(skip, str) else None)
    if not _keep(raw, kind, user_id):
        return None, root, needs_thread
    mapped = _fields(
        session,
        account,
        token,
        user_id,
        channel_id,
        kind,
        raw,
        names,
    )
    return mapped, root, needs_thread


def _replies(
    session: Session,
    account: ConnectedAccount,
    token: str,
    user_id: str,
    channel_id: str,
    kind: str,
    root_ts: str,
    state: dict,
    names: dict[str, str],
    budget: int,
    high: str | None,
) -> tuple[list[dict], str | None, int, bool, str | None]:
    collected: list[dict] = []
    reply_cursor = state.get("reply") if state.get("thread") == root_ts else None
    finished = True
    while budget > 0:
        params = {
            "channel": channel_id,
            "ts": root_ts,
            "limit": "100",
            "inclusive": "false",
            "oldest": state["oldest"],
        }
        if isinstance(reply_cursor, str) and reply_cursor:
            params["cursor"] = reply_cursor
        payload = slack_api(token, "conversations.replies", params=params)
        budget -= 1
        rows = payload.get("messages")
        if isinstance(rows, list):
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                mapped, _root, _needs = _consider(
                    session, account, token, user_id, channel_id, kind, raw, state, names
                )
                if mapped is not None:
                    collected.append(mapped)
                    ts = mapped["graph_id"].split(":", 1)[-1]
                    high = _ts_max(high, ts)
        reply_cursor = _next_cursor(payload)
        if not reply_cursor:
            finished = True
            break
        finished = False
        if budget <= 0:
            break
    return collected, high, budget, finished, reply_cursor if not finished else None


def _keep(raw: dict, kind: str, user_id: str) -> bool:
    if kind in {"im", "mpim"}:
        return True
    return f"<@{user_id}>" in _blob(raw)


def _is_root(raw: dict) -> bool:
    thread = raw.get("thread_ts")
    ts = raw.get("ts")
    return not isinstance(thread, str) or thread == ts


def _replies_newer(raw: dict, skip: str | None) -> bool:
    count = raw.get("reply_count")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return False
    latest = raw.get("latest_reply")
    if skip and isinstance(latest, str) and _ts_leq(latest, skip):
        return False
    return True


def _fields(
    session: Session,
    account: ConnectedAccount,
    token: str,
    user_id: str,
    channel_id: str,
    kind: str,
    raw: dict,
    names: dict[str, str],
) -> dict | None:
    ts = raw.get("ts")
    if not isinstance(ts, str):
        return None
    received = _received_at(ts)
    if received is None:
        return None
    thread_ts = raw.get("thread_ts") if isinstance(raw.get("thread_ts"), str) else None
    reply_thread = thread_ts or ts
    if len(reply_thread) > 32:
        reply_thread = ts
    text = _decode(_blob(raw))[:_BODY_CHARS]
    sender = raw.get("user") if isinstance(raw.get("user"), str) else None
    if kind in {"im", "mpim"}:
        conversation = channel_id
    else:
        conversation = thread_ts or ts
    return {
        "graph_id": f"{channel_id}:{ts}"[:512],
        "subject": None,
        "body_preview": text[:_PREVIEW_CHARS] or None,
        "body_text": text or None,
        "from_name": _display_name(session, account, token, sender, names) if sender else "Slack",
        "from_email": None,
        "is_read": None,
        "received_at": received,
        "conversation_id": conversation[:512],
        "to_recipients": None,
        "cc_recipients": None,
        "reply_to": None,
        "direction": "outgoing" if sender == user_id else "incoming",
        "origin": "provider",
        "slack_channel_id": channel_id,
        "slack_thread_ts": reply_thread,
        "slack_permalink": _permalink(channel_id, ts, thread_ts),
    }


def _display_name(
    session: Session,
    account: ConnectedAccount,
    token: str,
    slack_user_id: str,
    names: dict[str, str],
) -> str:
    if slack_user_id in names:
        return names[slack_user_id]
    cached = session.exec(
        select(SlackUserCache).where(
            SlackUserCache.connected_account_id == account.id,
            SlackUserCache.slack_user_id == slack_user_id,
        )
    ).first()
    fresh_after = utcnow() - _NAME_TTL
    if cached is not None and cached.fetched_at >= fresh_after and cached.display_name:
        names[slack_user_id] = cached.display_name
        return cached.display_name
    name = slack_user_id
    try:
        payload = slack_api(token, "users.info", params={"user": slack_user_id})
    except SlackApiError as exc:
        logger.warning("Slack users.info failed error=%s", exc.code)
    else:
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
        for value in (profile.get("display_name"), user.get("real_name"), user.get("name")):
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
    now = utcnow()
    if cached is None:
        cached = SlackUserCache(
            connected_account_id=account.id,
            slack_user_id=slack_user_id[:32],
            display_name=name,
            fetched_at=now,
        )
    else:
        cached.display_name = name
        cached.fetched_at = now
    session.add(cached)
    names[slack_user_id] = name
    return name


def _blob(raw: dict) -> str:
    parts: list[str] = []
    text = raw.get("text")
    if isinstance(text, str):
        parts.append(text)
    _collect_text(raw.get("blocks"), parts)
    return "\n".join(parts)


def _collect_text(value: object, parts: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "text" and isinstance(item, str):
                parts.append(item)
            else:
                _collect_text(item, parts)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, parts)


def _decode(text: str) -> str:
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _permalink(channel_id: str, ts: str, thread_ts: str | None) -> str:
    url = f"https://app.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        return f"{url}?thread_ts={thread_ts}"
    return url


def _next_cursor(payload: dict) -> str | None:
    meta = payload.get("response_metadata")
    if not isinstance(meta, dict):
        return None
    cursor = meta.get("next_cursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _received_at(ts: str) -> datetime | None:
    try:
        value = Decimal(ts)
    except InvalidOperation:
        return None
    seconds = int(value)
    micros = int((value - Decimal(seconds)) * Decimal(1_000_000))
    micros = min(999_999, max(0, micros))
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=micros)
    except (OverflowError, OSError, ValueError):
        return None


def _ts_leq(left: str, right: str) -> bool:
    try:
        return Decimal(left) <= Decimal(right)
    except InvalidOperation:
        return left <= right


def _ts_max(current: str | None, candidate: str) -> str:
    if current is None or not _ts_leq(candidate, current):
        return candidate
    return current
