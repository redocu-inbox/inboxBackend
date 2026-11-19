"""Slack connect, sync, scoring, and explicit send. The Slack API is mocked."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.message import Message
from app.models.slack import SlackSentMessage, SlackSyncCursor
from app.services.priority.score import score
from app.worker.mail_tasks import sync_connected_mailboxes, sync_mailbox_delta
from tests.test_frontend_connect import _Msal, _patch_redis, _user_id

_DM = "D12345678"
_MPIM = "G12345678"
_CHANNEL = "C12345678"
_USER = "U123"
_OTHER = "U999"
_DM_TS = "1763400000.000200"
_ROOT_TS = "1763400000.000300"
_REPLY_TS = "1763400000.000400"
_MPIM_TS = "1763400000.000500"
_STATE = "a" * 43


class _Response:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class SlackFake:
    """Scripted Slack Web API. Records every request and never opens a socket."""

    def __init__(self) -> None:
        self.calls: list = []
        self.sleeps: list[int] = []
        self.fail_list_once = False
        self.fail_list_always = False
        self._list_failed = False
        self.oauth_payload: dict = _user_token_payload()
        self.post_error: str | int | None = None
        self.post_forever_429 = False
        self.posted: dict | None = None
        self.revoked = False

    def __call__(self, request, timeout: int = 30):
        del timeout
        self.calls.append(request)
        path = urlsplit(request.full_url).path.removeprefix("/api/")
        if path == "oauth.v2.access":
            return _Response(self.oauth_payload)
        if path == "auth.revoke":
            self.revoked = True
            return _Response({"ok": True})
        if path == "users.info":
            user_id = parse_qs(urlsplit(request.full_url).query).get("user", [""])[0]
            name = "Ada" if user_id == _USER else "Luis"
            return _Response({"ok": True, "user": {"id": user_id, "real_name": name, "profile": {}}})
        if path == "conversations.list":
            if self.fail_list_always or (self.fail_list_once and not self._list_failed):
                self._list_failed = True
                raise HTTPError(request.full_url, 429, "ratelimited", {"Retry-After": "2"}, BytesIO(b"{}"))
            return _Response(
                {
                    "ok": True,
                    "channels": [
                        {"id": _DM, "is_im": True},
                        {"id": _MPIM, "is_mpim": True},
                        {"id": _CHANNEL, "is_channel": True, "is_member": True},
                    ],
                    "response_metadata": {"next_cursor": ""},
                }
            )
        if path == "conversations.history":
            channel = parse_qs(urlsplit(request.full_url).query).get("channel", [""])[0]
            return _Response({"ok": True, "messages": _history(channel), "response_metadata": {"next_cursor": ""}})
        if path == "conversations.replies":
            return _Response(
                {
                    "ok": True,
                    "messages": [
                        {"type": "message", "user": _OTHER, "text": "status update", "ts": _ROOT_TS, "reply_count": 1},
                        {
                            "type": "message",
                            "user": _OTHER,
                            "text": "<@U123> Please answer RFI 12 for the lobby by Friday.",
                            "ts": _REPLY_TS,
                            "thread_ts": _ROOT_TS,
                        },
                    ],
                    "response_metadata": {"next_cursor": ""},
                }
            )
        if path == "chat.postMessage":
            if self.post_forever_429:
                raise HTTPError(request.full_url, 429, "ratelimited", {"Retry-After": "3"}, BytesIO(b"{}"))
            if isinstance(self.post_error, str):
                return _Response({"ok": False, "error": self.post_error})
            self.posted = json.loads(request.data.decode())
            return _Response({"ok": True, "ts": "1763400099.000100", "channel": self.posted["channel"]})
        raise AssertionError(f"unexpected Slack call {path}")

def _user_token_payload() -> dict:
    return {
        "ok": True,
        "team": {"id": "T123", "name": "Acme"},
        "authed_user": {
            "id": _USER,
            "access_token": "xoxp-user-token",
            "token_type": "user",
            "scope": "im:history,im:read,mpim:history,mpim:read,channels:history,channels:read,groups:history,groups:read,users:read,chat:write",
        },
    }


def _history(channel: str) -> list[dict]:
    if channel == _DM:
        return [{"type": "message", "user": _OTHER, "text": "See you at the site", "ts": _DM_TS}]
    if channel == _MPIM:
        return [
            {
                "type": "message",
                "user": _OTHER,
                "text": "this is an automated notice, do not reply",
                "ts": _MPIM_TS,
            }
        ]
    if channel == _CHANNEL:
        return [
            {"type": "message", "user": _OTHER, "text": "hello everyone", "ts": "1763400000.000100"},
            {"type": "message", "user": _OTHER, "text": "<!channel> standup", "ts": "1763400000.000110"},
            {"type": "message", "user": _OTHER, "text": "<!here> looking", "ts": "1763400000.000120"},
            {
                "type": "message",
                "subtype": "channel_join",
                "user": _OTHER,
                "text": f"<@{_USER}> joined",
                "ts": "1763400000.000130",
            },
            {
                "type": "message",
                "user": _OTHER,
                "text": "status update",
                "ts": _ROOT_TS,
                "reply_count": 1,
                "latest_reply": _REPLY_TS,
            },
        ]
    return []


def _patch_slack(monkeypatch, fake: SlackFake) -> None:
    monkeypatch.setattr("app.services.slack.http.urlopen", fake)
    monkeypatch.setattr("app.services.slack.oauth.urlopen", fake)
    monkeypatch.setattr("app.services.slack.http.time.sleep", lambda seconds: fake.sleeps.append(seconds))


def _configure(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_client_id", "slack-client")
    monkeypatch.setattr(settings, "slack_client_secret", "slack-secret")
    monkeypatch.setattr(settings, "slack_redirect_uri", "https://redocu-api.onrender.com/auth/slack/callback")
    monkeypatch.setattr(settings, "slack_send_enabled", True)


def _clear_slack(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_client_id", "")
    monkeypatch.setattr(settings, "slack_client_secret", "")
    monkeypatch.setattr(settings, "slack_redirect_uri", "")


def _query(location: str) -> dict:
    return parse_qs(urlsplit(location).query)


def _slack_account(
    user_id: str,
    *,
    token: str = "xoxp-user-token",
    scopes: str = "chat:write,im:history",
    provider_account_id: str = "T123:U123",
    email: str = "Ada (Acme)",
) -> str:
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider="slack",
            provider_account_id=provider_account_id,
            tenant_id="T123",
            email=email,
            access_token=token,
            refresh_token=None,
            scopes=scopes,
            sync_status="idle",
            sync_generation=0,
        )
        session.add(account)
        session.commit()
        return str(account.id)


def _slack_message(user_id: str, account_id: str) -> str:
    with Session(engine) as session:
        message = Message(
            user_id=UUID(user_id),
            connected_account_id=UUID(account_id),
            graph_id=f"{_DM}:{_DM_TS}",
            body_text="See you at the site",
            body_preview="See you at the site",
            from_name="Luis",
            received_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            conversation_id=_DM,
            direction="incoming",
            origin="provider",
            slack_channel_id=_DM,
            slack_thread_ts=_DM_TS,
            slack_permalink=f"https://app.slack.com/archives/{_DM}/p1763400000000200",
        )
        session.add(message)
        session.commit()
        return str(message.id)


def _run_sync(account_id: str, generation: int = 0) -> None:
    asyncio.run(sync_mailbox_delta({"job_id": "slack-test"}, account_id, generation))


def _history_calls(fake: SlackFake) -> list[tuple[str, str]]:
    found = []
    for request in fake.calls:
        parts = urlsplit(request.full_url)
        if parts.path.endswith("conversations.history"):
            query = parse_qs(parts.query)
            found.append((query.get("channel", [""])[0], query.get("oldest", [""])[0]))
    return found


def test_slack_source_never_posts():
    source = Path("app/services/slack/sync.py").read_text()
    assert "chat.postMessage" not in source
    assert "send_slack" not in source


def test_unconfigured_slack_does_not_block_outlook(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    _clear_slack(monkeypatch)
    _patch_redis(monkeypatch)
    monkeypatch.setattr("app.api.auth.build_msal_app", lambda: _Msal())

    slack = client.get("/auth/slack/start", headers=headers)
    assert slack.status_code == 503
    assert slack.json()["detail"] == "Slack is not configured."

    outlook = client.get("/auth/microsoft/start", headers=headers)
    assert outlook.status_code == 200
    assert outlook.json()["authorization_url"].startswith("https://login.microsoftonline.com/")


def test_oauth_callback_stores_a_user_token_and_rejects_a_bot_token(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _configure(monkeypatch)
    fake = SlackFake()
    _patch_slack(monkeypatch, fake)
    _patch_redis(monkeypatch, payload=user_id.encode())

    start = client.get("/auth/slack/start", headers=headers)
    assert start.status_code == 200
    query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    assert "scope" not in query
    assert "chat:write" in query["user_scope"][0]
    assert "im:history" in query["user_scope"][0]
    assert headers["Authorization"].removeprefix("Bearer ") not in start.json()["authorization_url"]

    success = client.get(f"/auth/slack/callback?code=abc&state={_STATE}", follow_redirects=False)
    assert success.status_code == 302
    assert _query(success.headers["location"])["connected"] == ["1"]
    assert _query(success.headers["location"])["email"] == ["Ada (Acme)"]
    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"] is None
    assert me["mailboxes"][0]["provider"] == "slack"
    assert me["mailboxes"][0]["chat_write"] is True
    assert me["mailboxes"][0]["email"] == "Ada (Acme)"
    with Session(engine) as session:
        account = session.exec(select(ConnectedAccount).where(ConnectedAccount.provider == "slack")).one()
        assert account.provider_account_id == "T123:U123"
        assert account.access_token == "xoxp-user-token"
        assert account.refresh_token is None
        assert account.tenant_id == "T123"
        raw = session.execute(
            text("SELECT access_token FROM connected_accounts WHERE id = :id"),
            {"id": account.id},
        ).scalar_one()
    assert "xoxp-user-token" not in raw

    fake.oauth_payload = {
        "ok": True,
        "access_token": "xoxb-bot-token",
        "token_type": "bot",
        "team": {"id": "T999", "name": "Other"},
        "authed_user": {"id": "U999"},
    }
    _patch_redis(monkeypatch, payload=user_id.encode())
    denied = client.get(f"/auth/slack/callback?code=bot&state={_STATE}", follow_redirects=False)
    assert _query(denied.headers["location"])["error"] == ["slack_denied"]
    with Session(engine) as session:
        rows = session.exec(select(ConnectedAccount).where(ConnectedAccount.provider == "slack")).all()
        assert len(rows) == 1
        assert rows[0].access_token == "xoxp-user-token"


def test_sync_is_incremental_filters_mentions_and_dedupes(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    account_id = _slack_account(_user_id(client, headers))
    fake = SlackFake()
    _patch_slack(monkeypatch, fake)

    _run_sync(account_id)
    first_history = _history_calls(fake)
    assert {channel for channel, _oldest in first_history} == {_DM, _MPIM, _CHANNEL}
    floor = int((datetime.now(timezone.utc) - timedelta(days=14)).timestamp())
    assert all(abs(int(oldest) - floor) < 120 for _channel, oldest in first_history)
    assert any(request.full_url.find("conversations.replies") >= 0 for request in fake.calls)
    assert all("chat.postMessage" not in request.full_url for request in fake.calls)

    listed = client.get("/messages", headers=headers)
    assert listed.status_code == 200
    by_channel = {item["slack_channel_id"]: item for item in listed.json()}
    assert set(by_channel) == {_DM, _MPIM, _CHANNEL}
    assert by_channel[_DM]["provider"] == "slack"
    assert by_channel[_DM]["subject"] is None
    assert by_channel[_DM]["from_name"] == "Luis"
    assert by_channel[_DM]["from_email"] is None
    assert by_channel[_DM]["slack_thread_ts"] == _DM_TS
    assert by_channel[_DM]["slack_permalink"] == f"https://app.slack.com/archives/{_DM}/p{_DM_TS.replace('.', '')}"
    detail = client.get(f"/messages/{by_channel[_CHANNEL]['id']}", headers=headers).json()
    assert detail["body_text"] == "<@U123> Please answer RFI 12 for the lobby by Friday."
    assert by_channel[_CHANNEL]["category"] == "rfi"
    assert by_channel[_CHANNEL]["slack_thread_ts"] == _ROOT_TS
    assert "thread_ts=" in by_channel[_CHANNEL]["slack_permalink"]
    assert all(reason["code"] != "bulk_automated" for reason in by_channel[_CHANNEL]["priority_reasons"])
    assert all(reason["code"] != "bulk_automated" for reason in by_channel[_MPIM]["priority_reasons"])
    texts = [item["body_preview"] for item in listed.json()]
    assert "hello everyone" not in texts
    assert "<!channel> standup" not in texts
    assert all("joined" not in (item["body_preview"] or "") for item in listed.json())

    fake.calls.clear()
    _run_sync(account_id)
    second = _history_calls(fake)
    oldest_by_channel = {channel: oldest for channel, oldest in second}
    assert oldest_by_channel[_DM] == _DM_TS
    assert oldest_by_channel[_MPIM] == _MPIM_TS
    assert oldest_by_channel[_CHANNEL] == _REPLY_TS
    again = client.get("/messages", headers=headers).json()
    assert len(again) == 3
    with Session(engine) as session:
        assert len(session.exec(select(Message)).all()) == 3


def test_sync_retries_a_rate_limit_then_stores(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    account_id = _slack_account(_user_id(client, headers), provider_account_id="T123:U123")
    fake = SlackFake()
    fake.fail_list_once = True
    _patch_slack(monkeypatch, fake)

    _run_sync(account_id)

    assert fake.sleeps == [2]
    assert client.get("/messages", headers=headers).json()
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        assert account.sync_status == "idle"
        assert account.sync_error_code is None


def test_sync_stops_cleanly_when_rate_limits_continue(client, register_user, monkeypatch):
    account_id = _slack_account(_user_id(client, register_user("ana@example.com")))
    fake = SlackFake()
    fake.fail_list_always = True
    _patch_slack(monkeypatch, fake)

    _run_sync(account_id)

    assert fake.sleeps == [2, 2, 2]
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        assert account.sync_status == "failed"
        assert account.sync_error_code == "slack_rate_limited"
        assert session.exec(select(Message)).all() == []


def test_periodic_sync_includes_a_slack_token_without_a_refresh_token(client, register_user, monkeypatch):
    account_id = _slack_account(_user_id(client, register_user("ana@example.com")))
    seen: list[str] = []

    def _collect(session, account):
        seen.append(str(account.id))
        return []

    monkeypatch.setattr("app.worker.mail_tasks.collect_slack_messages", _collect)
    result = asyncio.run(sync_connected_mailboxes({"job_id": "cron"}))
    assert seen == [account_id]
    assert result["ran"] == 1
    assert result["errors"] == 0


def test_outlook_noreply_is_still_bulk_and_slack_text_is_not():
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    _score, _band, reasons, _stamp = score(
        {
            "subject": "Newsletter",
            "body_preview": "hi",
            "body_text": "hi",
            "from_email": "noreply@vendor.example",
            "from_name": "Vendor",
            "received_at": now,
        },
        now,
    )
    assert any(item["code"] == "bulk_automated" for item in reasons)
    _score, _band, slack_reasons, _stamp = score(
        {
            "subject": None,
            "body_preview": "this is an automated notice, do not reply",
            "body_text": "this is an automated notice, do not reply",
            "from_email": None,
            "from_name": "Luis",
            "received_at": now,
            "provider": "slack",
        },
        now,
    )
    assert all(item["code"] != "bulk_automated" for item in slack_reasons)


def test_thread_reply_and_new_send(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _slack_account(user_id)
    message_id = _slack_message(user_id, account_id)
    fake = SlackFake()
    _patch_slack(monkeypatch, fake)
    _configure(monkeypatch)

    reply = client.post(
        f"/messages/{message_id}/slack-reply",
        headers=headers,
        json={"text": "  On it.  "},
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["provider"] == "slack"
    assert body["body_text"] == "On it."
    assert body["slack_channel_id"] == _DM
    assert body["slack_thread_ts"] == _DM_TS
    assert fake.posted == {"channel": _DM, "text": "On it.", "thread_ts": _DM_TS}
    assert fake.calls[-1].get_header("Authorization") == "Bearer xoxp-user-token"
    with Session(engine) as session:
        log = session.exec(select(SlackSentMessage)).one()
        assert log.status == "sent"
        assert log.thread_ts == _DM_TS
        assert log.slack_ts == "1763400099.000100"
        stored = session.exec(select(Message).where(Message.graph_id == f"{_DM}:1763400099.000100")).one()
        assert stored.direction == "outgoing"
        assert stored.conversation_id == _DM

    sent = client.post(
        "/slack/send",
        headers=headers,
        json={"channel": _MPIM, "text": "New note"},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["slack_channel_id"] == _MPIM
    assert fake.posted == {"channel": _MPIM, "text": "New note"}
    with Session(engine) as session:
        assert len(session.exec(select(SlackSentMessage)).all()) == 2


def test_send_ownership_missing_scope_kill_switch_and_slack_errors(client, register_user, monkeypatch):
    owner = register_user("ana@example.com")
    other = register_user("bob@example.com")
    owner_id = _user_id(client, owner)
    account_id = _slack_account(owner_id, scopes="im:history,im:read")
    message_id = _slack_message(owner_id, account_id)
    fake = SlackFake()
    _patch_slack(monkeypatch, fake)
    monkeypatch.setattr(get_settings(), "slack_send_enabled", True)

    stolen = client.post(
        f"/messages/{message_id}/slack-reply",
        headers=other,
        json={"text": "nope"},
    )
    assert stolen.status_code == 404
    assert fake.calls == []

    missing = client.post("/slack/send", headers=other, json={"channel": _DM, "text": "nope"})
    assert missing.status_code == 404
    assert fake.calls == []

    scope = client.post(
        f"/messages/{message_id}/slack-reply",
        headers=owner,
        json={"text": "hello"},
    )
    assert scope.status_code == 409
    assert scope.json()["detail"] == "Reconnect Slack to allow sending."
    with Session(engine) as session:
        log = session.exec(select(SlackSentMessage)).one()
        assert log.status == "failed"
        assert log.error == "missing_scope"
    assert fake.calls == []

    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.scopes = "chat:write,im:history"
        session.add(account)
        session.commit()

    monkeypatch.setattr(get_settings(), "slack_send_enabled", False)
    blocked = client.post("/slack/send", headers=owner, json={"channel": _DM, "text": "hello"})
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Slack sending is turned off."
    assert fake.calls == []
    with Session(engine) as session:
        stopped = session.exec(
            select(SlackSentMessage).where(SlackSentMessage.error == "send_disabled")
        ).one()
        assert stopped.status == "failed"

    monkeypatch.setattr(get_settings(), "slack_send_enabled", True)
    fake.post_error = "missing_scope"
    again = client.post("/slack/send", headers=owner, json={"channel": _DM, "text": "hello"})
    assert again.status_code == 409
    assert again.json()["detail"] == "Reconnect Slack to allow sending."

    fake.post_error = "not_in_channel"
    denied = client.post("/slack/send", headers=owner, json={"channel": _CHANNEL, "text": "hello"})
    assert denied.status_code == 403

    fake.post_error = "channel_not_found"
    absent = client.post("/slack/send", headers=owner, json={"channel": _DM, "text": "hello"})
    assert absent.status_code == 404

    fake.post_error = None
    fake.post_forever_429 = True
    limited = client.post("/slack/send", headers=owner, json={"channel": _DM, "text": "hello"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "3"
    with Session(engine) as session:
        failed = session.exec(
            select(SlackSentMessage).where(SlackSentMessage.error == "ratelimited")
        ).all()
        assert failed

    blank = client.post("/slack/send", headers=owner, json={"channel": _DM, "text": "   "})
    assert blank.status_code == 422


def test_approve_does_not_send_slack_and_disconnect_hides_messages(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _configure(monkeypatch)
    fake = SlackFake()
    _patch_slack(monkeypatch, fake)
    _patch_redis(monkeypatch, payload=user_id.encode())
    connected = client.get(f"/auth/slack/callback?code=abc&state={_STATE}", follow_redirects=False)
    assert _query(connected.headers["location"])["connected"] == ["1"]
    with Session(engine) as session:
        account = session.exec(select(ConnectedAccount).where(ConnectedAccount.provider == "slack")).one()
        account_id = account.id
    message_id = _slack_message(user_id, str(account_id))

    def _explode(*args, **kwargs):
        raise AssertionError("graph send")

    monkeypatch.setattr("app.services.mail.send.urlopen", _explode)
    monkeypatch.setattr("app.services.inbox.approve.refresh_access_token", _explode)
    approved = client.post(f"/messages/{message_id}/approve", headers=headers)
    assert approved.status_code == 409
    assert approved.json()["detail"] == "Use the Slack reply endpoint."
    drafted = client.post(f"/messages/{message_id}/draft/outlook", headers=headers)
    assert drafted.status_code == 409

    listed = client.get("/messages", headers=headers)
    assert len(listed.json()) == 1
    disconnected = client.post("/auth/slack/disconnect", headers=headers)
    assert disconnected.status_code == 200
    assert disconnected.json()["disconnected"] is True
    assert fake.revoked is True
    assert client.get("/messages", headers=headers).json() == []
    assert client.get(f"/messages/{message_id}", headers=headers).status_code == 404
    me = client.get("/users/me", headers=headers).json()
    assert me["mailboxes"] == []
    with Session(engine) as session:
        account = session.get(ConnectedAccount, account_id)
        assert account.disconnected_at is not None
        assert account.access_token is None
        assert session.get(Message, UUID(message_id)) is not None
        assert session.exec(select(SlackSyncCursor)).all() == []
