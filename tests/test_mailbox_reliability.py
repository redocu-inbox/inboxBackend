"""OAuth redirects, mailbox isolation, Graph text, sync lifecycle, and logout."""

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.db import engine
from app.main import app
from app.models.connected_account import ConnectedAccount
from app.models.form_template import FormTemplate
from app.models.message import Message
from app.models.user import User
from app.services.google.http import GoogleMailboxIdentity
from app.services.google.messages import GmailRequestError, gmail_message_fields
from app.services.google.oauth import GMAIL_MODIFY_SCOPE, GoogleTokenUnavailable
from app.services.graph.tokens import TokenRefreshError
from app.services.graph.connect import ConnectStatus, connect_microsoft_mailbox
from app.services.graph.html_text import to_plain_text
from app.services.mailboxes import (
    ACTIVE_MAILBOX_INDEX,
    ONE_ACTIVE_MAILBOX_INDEX,
    SYNC_STALE_AFTER,
    deactivate_extra_microsoft_mailboxes,
)
from app.worker.mail_tasks import graph_message_fields, sync_mailbox_delta
from tests.test_frontend_connect import _Msal, _add_account, _add_message, _patch_redis, _user_id

HTML_BODY = "<html><body><p>Hello</p><script>alert(1)</script><style>p{}</style></body></html>"
GRAPH_ITEM = {
    "id": "graph-1",
    "subject": "Invoice <b>12</b>",
    "bodyPreview": "Hello there",
    "receivedDateTime": "2024-06-01T12:00:00Z",
    "from": {"emailAddress": {"name": "Ada <script>x</script>", "address": "ada@example.com"}},
    "isRead": False,
    "body": {"contentType": "html", "content": HTML_BODY},
}


def test_plain_text_drops_script_style_and_tags():
    assert to_plain_text(HTML_BODY) == "Hello"
    assert "<" not in to_plain_text(HTML_BODY)
    assert to_plain_text("Fish &amp; Chips") == "Fish & Chips"
    assert to_plain_text("  ") is None
    assert to_plain_text(None) is None
    assert to_plain_text("<script>alert(1)</script>") is None


def test_html_fragment_keeps_formatting_and_clickable_web_links_only():
    content = (
        '<style>p{display:none}</style><p onclick="bad()" style="color:red">'
        '<strong>Hello</strong> <a href="https://example.test/a">Web link</a> '
        '<a href="http://example.test/">HTTP link</a> '
        '<a href="java&#x73;cript:bad()">Unsafe</a> <a href="//example.test/">Relative</a> '
        '<img src="https://images.example.test/pixel" srcset="another-pixel">'
        '<svg><a href="https://images.example.test/">Hidden SVG</a></svg>'
        '<iframe src="https://images.example.test/">Hidden frame</iframe></p>'
    )
    fields = graph_message_fields({**GRAPH_ITEM, "body": {"contentType": "HTML", "content": content}})
    html = fields["body_html"]
    assert html.startswith("<p><strong>Hello</strong>")
    assert 'href="https://example.test/a"' in html
    assert 'href="http://example.test/"' in html
    assert ">Unsafe</a>" in html and ">Relative</a>" in html
    assert html.count("href=") == 2
    for unexpected in ("onclick", "style", "<img", "<svg", "<iframe", "images.example", "Hidden", "bad()"):
        assert unexpected not in html

    no_content = {"contentType": "html", "content": '<p><img src="https://images.example.test/pixel"></p>'}
    assert graph_message_fields({**GRAPH_ITEM, "body": no_content})["body_html"] is None
    assert graph_message_fields({**GRAPH_ITEM, "body": {"contentType": "text", "content": "Hi"}})["body_html"] is None
    assert "body_html" not in graph_message_fields({**GRAPH_ITEM, "body": None})


def test_graph_fields_are_plain_and_null_when_missing():
    fields = graph_message_fields(GRAPH_ITEM)
    assert fields["subject"] == "Invoice 12"
    assert fields["from_name"] == "Ada"
    assert fields["from_email"] == "ada@example.com"
    assert fields["is_read"] is False
    assert fields["body_text"] == "Hello"
    assert "<" not in fields["body_text"]
    sparse = graph_message_fields({"id": "g", "receivedDateTime": "2024-01-01T00:00:00Z"})
    assert sparse["from_name"] is None
    assert sparse["from_email"] is None
    assert sparse["is_read"] is None
    assert sparse["body_text"] is None
    assert graph_message_fields({"id": "g"}) is None


def test_frontend_url_rejects_open_redirect_and_header_injection():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, frontend_url="http://localhost:5173\r\nSet-Cookie: a=b")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, frontend_url="https://redocu.app/inbox")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, frontend_url="https://user:pass@redocu.app")
    Settings(_env_file=None, frontend_url="https://redocu.app")
    Settings(_env_file=None, frontend_url="http://localhost:5173/")


def test_second_microsoft_account_is_connected_without_touching_the_first(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="first@outlook.com", oid="oid-first", access_token="keep-me")
    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "id_token_claims": {"oid": "oid-second", "preferred_username": "second@outlook.com"},
            }
        ),
    )
    response = client.get(
        "/auth/microsoft/callback?code=abc&state=" + ("b" * 43),
        follow_redirects=False,
    )
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["connected"] == ["1"]
    (second_id,) = query["mailbox_id"]
    assert second_id != account_id
    assert "new-access" not in response.headers["location"]
    assert "new-refresh" not in response.headers["location"]
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", second_id, 1)
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        assert account.email == "first@outlook.com"
        assert account.access_token == "keep-me"
        assert account.disconnected_at is None
        assert session.get(ConnectedAccount, UUID(second_id)).access_token == "new-access"


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_same_provider_connect_reconnect_sync_and_disconnect_are_isolated(client, register_user, monkeypatch, provider):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    revoked = []
    monkeypatch.setattr("app.api.auth.revoke_google_token", revoked.append)
    monkeypatch.setattr("app.api.mailboxes.revoke_google_token", revoked.append)
    monkeypatch.setattr("app.worker.mail_tasks._ensure_graph_subscription", lambda *_: None)

    def callback(identity, token, target=None, *, gmail_access=True):
        email = f"{identity}@example.com"
        state = user_id if target is None else f"{provider}:{user_id}:{target}"
        _patch_redis(monkeypatch, state.encode())
        if provider == "google":
            monkeypatch.setattr("app.api.auth.exchange_authorization_code", lambda _: {
                "access_token": token, "refresh_token": f"refresh-{token}",
                "expires_in": 3600, "scope": f"openid email {GMAIL_MODIFY_SCOPE}" if gmail_access else "openid email",
            })
            monkeypatch.setattr("app.api.auth.fetch_mailbox_identity", lambda _: GoogleMailboxIdentity(identity, email))
        else:
            monkeypatch.setattr("app.api.auth.build_msal_app", lambda: _Msal({
                "access_token": token, "refresh_token": f"refresh-{token}", "expires_in": 3600,
                "scope": "Mail.ReadWrite Mail.Send", "id_token_claims": {"oid": identity, "preferred_username": email},
            }))
        response = client.get(f"/auth/{provider}/callback", params={"code": "abc", "state": "s" * 43}, follow_redirects=False)
        assert response.status_code == 302
        return parse_qs(urlsplit(response.headers["location"]).query)

    (first,) = callback("first", "access-first")["mailbox_id"]
    (second,) = callback("second", "access-second")["mailbox_id"]
    assert second != first
    assert callback("first", "wrong", second)["error"] == ["mailbox_mismatch"]
    if provider == "google":
        assert callback("first", "incomplete", second, gmail_access=False)["error"] == ["google_denied"]
        assert revoked == []
    assert callback("second", "renewed-second", second)["mailbox_id"] == [second]
    assert [call.args[1:] for call in app.state.arq.enqueue_job.await_args_list] == [(first, 1), (second, 1), (second, 2)]
    profile = client.get("/users/me", headers=headers).json()
    assert [(row["id"], row["email"]) for row in profile["mailboxes"]] == [
        (first, "first@example.com"), (second, "second@example.com"),
    ]

    legacy = client.post(f"/auth/{provider}/disconnect", headers=headers)
    assert (legacy.status_code, legacy.json()) == (409, {"detail": "choose_mailbox"})
    assert revoked == []

    disconnect_in_flight = False
    tokens = []

    def fetch(token):
        tokens.append(token)
        message_id = "same-provider-id"
        if disconnect_in_flight and token == "access-first":
            assert client.post(f"/mailboxes/{first}/disconnect", headers=headers).status_code == 200
            message_id = "would-resurrect"
        if provider == "google":
            return [_gmail_item(message_id=message_id, unread=token == "access-first")]
        return {"value": [{**GRAPH_ITEM, "id": message_id, "isRead": token != "access-first"}]}

    monkeypatch.setattr("app.worker.mail_tasks.collect_gmail_messages", fetch)
    monkeypatch.setattr("app.worker.mail_tasks._graph_get", lambda _url, token: fetch(token))
    asyncio.run(sync_mailbox_delta({}, first, 1))
    asyncio.run(sync_mailbox_delta({}, second, 2))
    saved = {row["account_id"]: row for row in client.get("/messages", headers=headers).json()}
    assert set(saved) == {first, second}
    assert saved[first]["is_read"] is False and saved[second]["is_read"] is True
    assert tokens == ["access-first", "renewed-second"]

    disconnect_in_flight = True
    asyncio.run(sync_mailbox_delta({}, first, 1))
    profile = client.get("/users/me", headers=headers).json()
    (still_connected,) = profile["mailboxes"]
    assert still_connected["id"] == second and still_connected["sync"]["status"] == "idle"
    assert [row["id"] for row in client.get("/messages", headers=headers).json()] == [saved[second]["id"]]
    assert revoked == (["refresh-access-first"] if provider == "google" else [])
    with Session(engine) as session:
        assert session.get(ConnectedAccount, UUID(first)).access_token is None
        assert session.get(ConnectedAccount, UUID(second)).access_token == "renewed-second"
        assert session.get(Message, UUID(saved[first]["id"])) is not None
        assert session.scalar(select(Message.id).where(Message.graph_id == "would-resurrect")) is None


def test_manual_sync_attempts_the_next_mailbox_when_one_enqueue_fails(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    ids = sorted([
        _add_account(user_id, provider="google", email="first@gmail.com"),
        _add_account(user_id, provider="google", email="second@gmail.com"),
    ])

    async def enqueue(_job, account_id, _generation):
        if account_id == ids[0]:
            raise ConnectionError("queue failed for the first mailbox")

    app.state.arq.enqueue_job.side_effect = enqueue
    response = client.post("/messages/sync", headers=headers)
    assert response.status_code == 202
    assert response.json() == {"enqueued": True, "sync_status": "failed"}
    assert [call.args[1] for call in app.state.arq.enqueue_job.await_args_list] == ids
    syncs = {row["id"]: row["sync"] for row in client.get("/users/me", headers=headers).json()["mailboxes"]}
    assert syncs[ids[0]]["error_code"] == "queue_unavailable"
    assert syncs[ids[1]]["status"] == "pending"


def test_reconnect_same_oid_updates_the_one_row(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(
        user_id, email="old@outlook.com", oid="oid-same", access_token="old-access"
    )
    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "Mail.Read",
                "id_token_claims": {
                    "oid": "oid-same",
                    "preferred_username": "new@outlook.com",
                    "tid": "tenant-9",
                },
            }
        ),
    )
    response = client.get(
        "/auth/microsoft/callback?code=abc&state=" + ("c" * 43),
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "connected=1" in response.headers["location"]
    assert urlsplit(response.headers["location"]).hostname == "localhost"
    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"] == {
        "id": account_id,
        "connected": True,
        "provider": "microsoft",
        "email": "new@outlook.com",
    }
    assert me["sync"]["status"] == "pending"
    assert me["sync"]["last_successful_at"] is None
    with Session(engine) as session:
        rows = session.exec(select(ConnectedAccount)).all()
        assert len(rows) == 1
        assert rows[0].access_token == "new-access"
        assert rows[0].tenant_id == "tenant-9"


def test_redis_outage_does_not_exchange_the_code_or_leak_errors(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")

    def unavailable(*args, **kwargs):
        raise ConnectionError("redis down at secret-host")

    monkeypatch.setattr("app.api.auth.redis.Redis.from_url", unavailable)
    start = client.get("/auth/microsoft/start", headers=headers)
    assert start.status_code == 503
    assert start.json() == {"detail": "Outlook connect is temporarily unavailable."}
    assert "secret-host" not in start.text
    assert "redis" not in start.text.lower()

    called = {"msal": False}

    class Guard:
        def acquire_token_by_authorization_code(self, code, scopes, redirect_uri):
            called["msal"] = True
            return {}

    monkeypatch.setattr("app.api.auth.build_msal_app", lambda: Guard())
    callback = client.get(
        "/auth/microsoft/callback?code=super-secret-code&state=" + ("d" * 43),
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert parse_qs(urlsplit(callback.headers["location"]).query)["error"] == ["invalid_state"]
    assert "super-secret-code" not in callback.headers["location"]
    assert called["msal"] is False


def test_enqueue_failure_keeps_tokens_and_reports_connected(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "saved-access",
                "refresh_token": "saved-refresh",
                "expires_in": 3600,
                "id_token_claims": {"oid": "oid-saved", "preferred_username": "saved@outlook.com"},
            }
        ),
    )
    app.state.arq.enqueue_job.side_effect = RuntimeError("queue down")
    response = client.get(
        "/auth/microsoft/callback?code=abc&state=" + ("e" * 43),
        follow_redirects=False,
    )
    app.state.arq.enqueue_job.side_effect = None
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["connected"] == ["1"]
    assert "queue" not in response.headers["location"]
    assert "saved-access" not in response.headers["location"]
    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"]["email"] == "saved@outlook.com"
    assert me["sync"] == {
        "status": "failed",
        "last_successful_at": None,
        "error_code": "queue_unavailable",
    }
    with Session(engine) as session:
        account = session.exec(select(ConnectedAccount)).one()
        assert account.access_token == "saved-access"
        assert account.refresh_token == "saved-refresh"


def test_newline_email_does_not_split_the_redirect(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "id_token_claims": {
                    "oid": "oid-nl",
                    "preferred_username": "ana@outlook.com\r\nX-Injected: yes",
                },
            }
        ),
    )
    response = client.get(
        "/auth/microsoft/callback?code=abc&state=" + ("f" * 43),
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert response.status_code == 302
    assert "\r" not in location and "\n" not in location
    assert parse_qs(urlsplit(location).query)["error"] == ["token_exchange"]
    with Session(engine) as session:
        assert session.exec(select(ConnectedAccount)).first() is None


def _future():
    return datetime.now(timezone.utc) + timedelta(hours=2)


def _run_sync(account_id: str, generation: int) -> None:
    asyncio.run(sync_mailbox_delta({"job_id": "job-1"}, account_id, generation))


def test_gmail_sync_maps_mail_without_calling_graph(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider="google",
            provider_account_id="sub-sync",
            email="ana@gmail.com",
            access_token="google-access-token",
            refresh_token="google-refresh-token",
            token_expires_at=_future(),
            sync_generation=1,
            sync_status="idle",
        )
        session.add(account)
        session.commit()
        account_id = account.id

    queued = client.post("/messages/sync", headers=headers)
    assert queued.status_code == 202
    assert queued.json() == {"enqueued": True, "sync_status": "pending"}

    def _forbid(*args, **kwargs):
        raise AssertionError("Google token was sent to Graph")

    monkeypatch.setattr("app.worker.mail_tasks._graph_get", _forbid)
    monkeypatch.setattr("app.worker.mail_tasks.refresh_access_token", _forbid)
    monkeypatch.setattr(
        "app.worker.mail_tasks.collect_gmail_messages",
        lambda token: [_gmail_item(message_id="gmail-1", unread=True), _gmail_item(message_id="gmail-2", unread=False)],
    )
    _run_sync(str(account_id), 2)
    listed = client.get("/messages", headers=headers)
    assert listed.status_code == 200
    assert "<" not in listed.text
    rows = listed.json()
    assert {row["is_read"] for row in rows} == {True, False}
    assert {row["from_email"] for row in rows} == {"ada@example.com"}
    assert {row["from_name"] for row in rows} == {"Ada"}
    assert {row["subject"] for row in rows} == {"Invoice 12"}
    assert {row["body_preview"] for row in rows} == {"Hello there"}
    unread = next(row for row in rows if row["is_read"] is False)
    detail = client.get(f"/messages/{unread['id']}", headers=headers).json()
    assert detail["body_text"] == "Hello"
    assert detail["draft"] is None
    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"]["provider"] == "google"
    assert me["sync"]["status"] == "idle"
    assert me["sync"]["error_code"] is None
    assert me["sync"]["last_successful_at"] is not None
    with Session(engine) as session:
        saved = session.get(ConnectedAccount, account_id)
        assert saved is not None
        assert saved.access_token == "google-access-token"
        assert saved.refresh_token == "google-refresh-token"
        kept = saved.last_sync_at

    monkeypatch.setattr(
        "app.services.google.tokens.exchange_refresh_token",
        lambda refresh_token: (_ for _ in ()).throw(TokenRefreshError("invalid_grant")),
    )
    monkeypatch.setattr(
        "app.worker.mail_tasks.collect_gmail_messages",
        lambda token: (_ for _ in ()).throw(AssertionError("list after invalid_grant")),
    )
    with Session(engine) as session:
        account = session.get(ConnectedAccount, account_id)
        account.token_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        account.sync_generation = 3
        account.sync_status = "pending"
        session.add(account)
        session.commit()
    _run_sync(str(account_id), 3)
    failed = client.get("/users/me", headers=headers).json()["sync"]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "token_refresh_failed"
    assert failed["last_successful_at"] == me["sync"]["last_successful_at"]
    with Session(engine) as session:
        assert session.get(ConnectedAccount, account_id).last_sync_at == kept

    calls = {"n": 0}

    def _unauthorized_once(token):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GmailRequestError(401)
        assert token == "new-access"
        return [_gmail_item(message_id="gmail-1", unread=False)]

    monkeypatch.setattr("app.worker.mail_tasks.collect_gmail_messages", _unauthorized_once)
    monkeypatch.setattr(
        "app.services.google.tokens.exchange_refresh_token",
        lambda refresh_token: {"access_token": "new-access", "expires_in": 3600},
    )
    with Session(engine) as session:
        account = session.get(ConnectedAccount, account_id)
        account.token_expires_at = _future()
        account.sync_generation = 4
        account.sync_status = "pending"
        session.add(account)
        session.commit()
    _run_sync(str(account_id), 4)
    again = client.get("/users/me", headers=headers).json()["sync"]
    assert again["status"] == "idle"
    assert again["error_code"] is None
    with Session(engine) as session:
        saved = session.get(ConnectedAccount, account_id)
        assert saved.access_token == "new-access"
        assert saved.refresh_token == "google-refresh-token"
    assert calls["n"] == 2

    monkeypatch.setattr(
        "app.services.google.tokens.exchange_refresh_token",
        lambda refresh_token: (_ for _ in ()).throw(GoogleTokenUnavailable()),
    )
    monkeypatch.setattr(
        "app.worker.mail_tasks.collect_gmail_messages",
        lambda token: (_ for _ in ()).throw(AssertionError("list during google outage")),
    )
    with Session(engine) as session:
        account = session.get(ConnectedAccount, account_id)
        kept_sync = account.last_sync_at
        account.token_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        account.sync_generation = 5
        account.sync_status = "pending"
        session.add(account)
        session.commit()
    _run_sync(str(account_id), 5)
    outage = client.get("/users/me", headers=headers).json()["sync"]
    assert outage["status"] == "failed"
    assert outage["error_code"] == "gmail_unavailable"
    assert outage["last_successful_at"] == again["last_successful_at"]
    with Session(engine) as session:
        saved = session.get(ConnectedAccount, account_id)
        assert saved.last_sync_at == kept_sync
        assert saved.refresh_token == "google-refresh-token"


def test_google_token_post_reconnects_only_for_invalid_grant(monkeypatch, caplog):
    import logging
    from email.message import EmailMessage
    from io import BytesIO
    from urllib.error import HTTPError, URLError

    from app.services.google.oauth import _google_token_post

    caplog.set_level(logging.WARNING)
    secret = "refresh-token-secret"

    class _Response:
        def __init__(self, body: bytes):
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    def _http(code: int, body: bytes) -> HTTPError:
        return HTTPError(
            "https://oauth2.googleapis.com/token",
            code,
            "error",
            EmailMessage(),
            BytesIO(body),
        )

    def _open(error: Exception):
        def _urlopen(request, timeout):
            assert secret.encode() in request.data
            raise error

        return _urlopen

    monkeypatch.setattr(
        "app.services.google.oauth.urlopen",
        lambda request, timeout: _Response(b'{"access_token":"access","expires_in":3600}'),
    )
    assert _google_token_post({"refresh_token": secret})["access_token"] == "access"

    monkeypatch.setattr(
        "app.services.google.oauth.urlopen",
        _open(_http(400, b'{"error":"invalid_grant","error_description":"Token has been revoked."}')),
    )
    with pytest.raises(TokenRefreshError):
        _google_token_post({"refresh_token": secret, "grant_type": "refresh_token"})

    monkeypatch.setattr(
        "app.services.google.oauth.urlopen",
        _open(_http(400, b'{"error":"invalid_client"}')),
    )
    with pytest.raises(GoogleTokenUnavailable):
        _google_token_post({"refresh_token": secret, "grant_type": "refresh_token"})

    monkeypatch.setattr(
        "app.services.google.oauth.urlopen",
        _open(_http(503, b'{"error":"invalid_grant"}')),
    )
    with pytest.raises(GoogleTokenUnavailable):
        _google_token_post({"refresh_token": secret, "grant_type": "refresh_token"})

    monkeypatch.setattr(
        "app.services.google.oauth.urlopen",
        _open(_http(500, b"upstream")),
    )
    with pytest.raises(GoogleTokenUnavailable):
        _google_token_post({"refresh_token": secret, "grant_type": "refresh_token"})

    monkeypatch.setattr(
        "app.services.google.oauth.urlopen",
        _open(URLError("timed out")),
    )
    with pytest.raises(GoogleTokenUnavailable):
        _google_token_post({"refresh_token": secret, "grant_type": "refresh_token"})

    assert secret not in caplog.text
    assert "revoked" not in caplog.text


def test_google_disconnect_revokes_and_hides_mail(client, register_user, monkeypatch, caplog):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@gmail.com", oid="sub-ana", provider="google")
    message_id = _add_message(
        user_id,
        account_id,
        graph_id="kept",
        received_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
        priority_score=None,
    )
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.access_token = "google-access-token"
        account.refresh_token = "google-refresh-token"
        session.add(account)
        session.commit()

    seen: list[str] = []
    monkeypatch.setattr("app.api.auth.revoke_google_token", seen.append)
    assert client.post("/auth/microsoft/disconnect", headers=headers).status_code == 404
    assert client.get("/users/me", headers=headers).json()["mailbox"]["email"] == "ana@gmail.com"

    gone = client.post("/auth/google/disconnect", headers=headers)
    assert gone.status_code == 200
    assert gone.json() == {"disconnected": True}
    assert seen == ["google-refresh-token"]
    assert "google-refresh-token" not in caplog.text
    assert "google-access-token" not in caplog.text
    assert client.get("/users/me", headers=headers).json()["mailbox"] is None
    assert client.get("/messages", headers=headers).json() == []
    assert client.get(f"/messages/{message_id}", headers=headers).status_code == 404
    with Session(engine) as session:
        saved = session.get(ConnectedAccount, UUID(account_id))
        assert saved.disconnected_at is not None
        assert saved.access_token is None
        assert saved.refresh_token is None
        assert session.get(Message, UUID(message_id)) is not None
    assert client.post("/auth/google/disconnect", headers=headers).status_code == 404
    assert client.post("/messages/sync", headers=headers).status_code == 404


def _gmail_item(*, message_id: str, unread: bool) -> dict:
    import base64

    def encode(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")

    labels = ["INBOX", "UNREAD"] if unread else ["INBOX"]
    return {
        "id": message_id,
        "internalDate": "1717243200000" if unread else "1717156800000",
        "labelIds": labels,
        "snippet": "Hello there",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Invoice <b>12</b>"},
                {"name": "From", "value": "Ada <script>x</script> <ada@example.com>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode("Hello")}},
                {"mimeType": "text/html", "body": {"data": encode(HTML_BODY)}},
            ],
        },
    }


def test_gmail_html_only_body_is_plain_text():
    item = _gmail_item(message_id="gmail-html", unread=False)
    item["payload"]["parts"] = [
        part for part in item["payload"]["parts"] if part["mimeType"] == "text/html"
    ]
    fields = gmail_message_fields(item)
    assert fields is not None
    assert fields["body_text"] == "Hello"
    assert "<" not in fields["body_text"]
    assert fields["is_read"] is True
    assert fields["from_email"] == "ada@example.com"


def test_sync_success_failure_and_superseded_generation(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    previous = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider_account_id="oid-sync",
            email="ana@outlook.com",
            access_token="access",
            refresh_token="refresh",
            token_expires_at=_future(),
            sync_generation=2,
            sync_status="pending",
            last_sync_at=previous,
        )
        session.add(account)
        session.commit()
        account_id = str(account.id)

    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: {"value": [GRAPH_ITEM]},
    )
    _run_sync(account_id, 1)
    with Session(engine) as session:
        untouched = session.get(ConnectedAccount, UUID(account_id))
        assert untouched.sync_status == "pending"
        assert untouched.last_sync_at == previous
        assert session.exec(select(Message)).first() is None

    _run_sync(account_id, 2)
    listed = client.get("/messages", headers=headers)
    assert listed.status_code == 200
    item = listed.json()[0]
    assert item["from_name"] == "Ada"
    assert item["from_email"] == "ada@example.com"
    assert item["is_read"] is False
    assert item["body_preview"] == "Hello there"
    assert item["priority_score"] == 26.0
    assert item["priority_band"] == "low"
    assert item["priority_reasons"] == [
        {"code": "keyword_payment", "label": "Payment", "weight": 12},
        {"code": "unread", "label": "Unread", "weight": 8},
        {"code": "category_invoice", "label": "Invoice", "weight": 6},
    ]
    assert item["category"] == "invoice"
    assert item["category_source"] == "rule"
    assert item["category_confidence"] == 1.0
    assert item["reference_number"] == "Invoice 12"
    assert "<" not in listed.text
    detail = client.get(f"/messages/{item['id']}", headers=headers).json()
    assert detail["body_text"] == "Hello"
    assert detail["draft"] is None
    me = client.get("/users/me", headers=headers).json()
    assert me["sync"]["status"] == "idle"
    assert me["sync"]["error_code"] is None
    assert me["sync"]["last_successful_at"] is not None

    monkeypatch.setattr(
        "app.worker.mail_tasks._graph_get",
        lambda url, token: (_ for _ in ()).throw(OSError("graph down")),
    )
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        account.sync_generation = 3
        account.sync_status = "pending"
        kept = account.last_sync_at
        session.add(account)
        session.commit()
    _run_sync(account_id, 3)
    failed = client.get("/users/me", headers=headers).json()["sync"]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "graph_unavailable"
    assert failed["last_successful_at"] == me["sync"]["last_successful_at"]
    with Session(engine) as session:
        assert session.get(ConnectedAccount, UUID(account_id)).last_sync_at == kept


def test_manual_sync_coalesces_and_queue_failure_is_not_success(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _add_account(user_id)
    first = client.post("/messages/sync", headers=headers)
    assert first.status_code == 202
    assert first.json() == {"enqueued": True, "sync_status": "pending"}
    assert client.get("/users/me", headers=headers).json()["sync"]["last_successful_at"] is None
    second = client.post("/messages/sync", headers=headers)
    assert second.status_code == 202
    assert second.json()["enqueued"] is False
    assert second.json()["sync_status"] == "pending"
    assert app.state.arq.enqueue_job.await_count == 1

    with Session(engine) as session:
        account = session.exec(select(ConnectedAccount)).one()
        account.sync_status = "pending"
        account.sync_status_at = datetime.now(timezone.utc) - SYNC_STALE_AFTER - timedelta(minutes=1)
        session.add(account)
        session.commit()
    stale = client.post("/messages/sync", headers=headers)
    assert stale.status_code == 202
    assert stale.json()["enqueued"] is True
    assert app.state.arq.enqueue_job.await_count == 2

    app.state.arq.enqueue_job.side_effect = RuntimeError("nope")
    with Session(engine) as session:
        account = session.exec(select(ConnectedAccount)).one()
        account.sync_status = "idle"
        session.add(account)
        session.commit()
    unavailable = client.post("/messages/sync", headers=headers)
    app.state.arq.enqueue_job.side_effect = None
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Mailbox sync could not be queued."}
    sync = client.get("/users/me", headers=headers).json()["sync"]
    assert sync["status"] == "failed"
    assert sync["error_code"] == "queue_unavailable"
    assert sync["last_successful_at"] is None


def test_disconnect_hides_mail_blocks_jobs_and_reconnects_same_account(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    other = register_user("bob@example.com")
    user_id = _user_id(client, headers)
    account_id = _add_account(user_id, email="ana@outlook.com", oid="oid-ana", access_token="live-access")
    message_id = _add_message(
        user_id,
        account_id,
        graph_id="kept",
        received_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
        priority_score=None,
    )
    with Session(engine) as session:
        user = session.get(User, UUID(user_id))
        session.add(
            FormTemplate(
                user_id=user.id,
                filename="form.pdf",
                storage_path="forms/x",
                mime_type="application/pdf",
                tags=[],
                form_schema={},
            )
        )
        account = session.get(ConnectedAccount, UUID(account_id))
        account.refresh_token = "live-refresh"
        account.token_expires_at = _future()
        account.sync_generation = 4
        account.sync_status = "running"
        session.add(account)
        session.commit()

    calls = {"graph": 0}

    def graph_get(url, token):
        calls["graph"] += 1
        with Session(engine) as session:
            from app.services.mailboxes import disconnect_active_mailbox

            disconnect_active_mailbox(session, UUID(user_id))
        return {"value": [GRAPH_ITEM]}

    monkeypatch.setattr("app.worker.mail_tasks._graph_get", graph_get)
    _run_sync(account_id, 4)
    assert calls["graph"] == 1
    with Session(engine) as session:
        account = session.get(ConnectedAccount, UUID(account_id))
        assert account.disconnected_at is not None
        assert account.access_token is None
        assert account.refresh_token is None
        assert session.get(Message, UUID(message_id)) is not None
        assert session.exec(select(FormTemplate)).one().filename == "form.pdf"

    assert client.get("/messages", headers=headers).json() == []
    assert client.get(f"/messages/{message_id}", headers=headers).status_code == 404
    assert client.get("/users/me", headers=headers).json()["mailbox"] is None
    assert client.post("/messages/sync", headers=headers).status_code == 404
    assert client.post("/auth/microsoft/disconnect", headers=other).status_code == 404
    assert client.get("/users/me", headers=other).json()["email"] == "bob@example.com"

    calls["graph"] = 0
    _run_sync(account_id, 4)
    assert calls["graph"] == 0

    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "again-access",
                "refresh_token": "again-refresh",
                "expires_in": 3600,
                "id_token_claims": {"oid": "oid-ana", "preferred_username": "ana@outlook.com"},
            }
        ),
    )
    back = client.get(
        "/auth/microsoft/callback?code=abc&state=" + ("g" * 43),
        follow_redirects=False,
    )
    assert "connected=1" in back.headers["location"]
    restored = client.get("/messages", headers=headers).json()
    assert [row["id"] for row in restored] == [message_id]
    with Session(engine) as session:
        assert session.exec(select(ConnectedAccount)).one().access_token == "again-access"


def test_logout_revokes_only_that_jti_and_legacy_tokens_follow_iat(client, register_user):
    ana = register_user("ana@example.com")
    bob = register_user("bob@example.com")
    again = client.post(
        "/auth/login", json={"email": "ana@example.com", "password": "correct-horse-battery"}
    )
    second = {"Authorization": f"Bearer {again.json()['access_token']}"}
    assert client.post("/auth/logout", headers=ana).status_code == 204
    assert client.get("/users/me", headers=ana).status_code == 401
    assert client.get("/users/me", headers=second).status_code == 200
    assert client.get("/users/me", headers=bob).status_code == 200

    user_id = client.get("/users/me", headers=second).json()["id"]
    now = datetime.now(timezone.utc)
    key = get_settings().secret_key
    # Both iat values are in the past. PyJWT rejects an iat in the future.
    older = jwt.encode(
        {"sub": user_id, "iat": now - timedelta(minutes=10), "exp": now + timedelta(hours=1)},
        key,
        algorithm="HS256",
    )
    newer = jwt.encode(
        {"sub": user_id, "iat": now - timedelta(minutes=1), "exp": now + timedelta(hours=1)},
        key,
        algorithm="HS256",
    )
    older_headers = {"Authorization": f"Bearer {older}"}
    newer_headers = {"Authorization": f"Bearer {newer}"}
    assert client.get("/users/me", headers=older_headers).status_code == 200
    assert client.post("/auth/logout", headers=older_headers).status_code == 204
    assert client.get("/users/me", headers=older_headers).status_code == 401
    assert client.get("/users/me", headers=newer_headers).status_code == 200
    assert client.get("/users/me", headers=second).status_code == 200
    assert client.get("/users/me", headers=bob).status_code == 200


@pytest.mark.parametrize("same_identity", [False, True])
def test_concurrent_connect_uses_provider_identity(same_identity):
    with Session(engine) as session:
        user = User(email=f"{uuid4()}@example.com", hashed_password="x")
        session.add(user)
        session.commit()
        user_id = user.id

    import threading

    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(oid: str, email: str) -> None:
        barrier.wait()
        with Session(engine) as session:
            outcomes.append(
                connect_microsoft_mailbox(
                    session,
                    user_id=user_id,
                    provider_account_id=oid,
                    tenant_id="t",
                    email=email,
                    access_token=f"access-{oid}",
                    refresh_token=f"refresh-{oid}",
                    expires_at=_future(),
                    scopes="Mail.Read",
                ).status
            )

    threads = [
        threading.Thread(target=attempt, args=("oid-a", "a@outlook.com")),
        threading.Thread(target=attempt, args=("oid-a" if same_identity else "oid-b", "b@outlook.com")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == [ConnectStatus.connected, ConnectStatus.connected]
    with Session(engine) as session:
        active = session.exec(
            select(ConnectedAccount).where(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.disconnected_at.is_(None),
            )
        ).all()
        expected = {"access-oid-a"} if same_identity else {"access-oid-a", "access-oid-b"}
        assert len(active) == len(expected)
        assert {row.access_token for row in active} == expected


def test_extra_mailbox_migration_preserves_tokens_and_messages():
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text(f"DROP INDEX IF EXISTS {ONE_ACTIVE_MAILBOX_INDEX}"))
        connection.execute(text(f"DROP INDEX IF EXISTS {ACTIVE_MAILBOX_INDEX}"))
        session = Session(bind=connection)
        user = User(email=f"{uuid4()}@example.com", hashed_password="x")
        session.add(user)
        session.flush()
        older = ConnectedAccount(
            user_id=user.id,
            provider_account_id="oid-older",
            email="older@outlook.com",
            access_token="token-old",
            refresh_token="refresh-old",
            updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        newer = ConnectedAccount(
            user_id=user.id,
            provider_account_id="oid-newer",
            email="newer@outlook.com",
            access_token="token-new",
            refresh_token="refresh-new",
            updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        session.add(older)
        session.add(newer)
        session.flush()
        session.add(
            Message(
                user_id=user.id,
                connected_account_id=older.id,
                graph_id="old-mail",
                subject="Keep me",
                received_at=datetime(2020, 2, 1, tzinfo=timezone.utc),
            )
        )
        session.flush()
        deactivate_extra_microsoft_mailboxes(connection)
        session.expire_all()
        kept = session.get(ConnectedAccount, newer.id)
        hidden = session.get(ConnectedAccount, older.id)
        assert kept.disconnected_at is None
        assert kept.access_token == "token-new"
        assert kept.refresh_token == "refresh-new"
        assert hidden.disconnected_at is not None
        assert hidden.access_token == "token-old"
        assert hidden.refresh_token == "refresh-old"
        assert session.exec(select(Message)).one().subject == "Keep me"
        transaction.rollback()


def test_cors_allows_authorization_and_content_type_only_for_listed_origins(client, register_user):
    headers = register_user("ana@example.com")
    origin = "http://localhost:5173"
    preflight = client.options(
        "/messages",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    allowed = preflight.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "content-type" in allowed
    assert preflight.headers["access-control-allow-origin"] == origin
    listed = client.get("/messages", headers={**headers, "Origin": origin})
    assert "x-total-count" in listed.headers["access-control-expose-headers"].lower()
    blocked = client.options(
        "/messages",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert blocked.status_code == 400
    assert blocked.headers.get("access-control-allow-origin") is None
