"""Milestone F: mailbox on the profile, sync, message contract, and the OAuth redirect."""

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlmodel import Session, select

from app.services.google.http import GoogleMailboxIdentity

from app.config import Settings, get_settings
from app.db import engine
from app.main import app
from app.models.connected_account import ConnectedAccount
from app.models.message import Message

LIST_KEYS = {
    "id",
    "subject",
    "body_preview",
    "from_name",
    "from_email",
    "received_at",
    "is_read",
    "provider",
    "conversation_id",
    "priority_score",
    "priority_band",
    "priority_reasons",
    "category",
    "category_source",
    "category_confidence",
    "project_name",
    "project_number",
    "due_date",
    "responsible_party",
    "reference_number",
    "user_corrected",
    "suggested_form",
}

# Frozen reply-draft object. created_at stays on the table and is not a key.
DRAFT_KEYS = {
    "id",
    "message_id",
    "status",
    "body",
    "source",
    "graph_draft_id",
    "error",
    "updated_at",
}


class _Redis:
    def __init__(self, payload: bytes | None = None):
        self.payload = payload
        self.saved: tuple | None = None

    def getdel(self, key: str):
        return self.payload

    def setex(self, key, ttl, value):
        self.saved = (key, ttl, value)

    def close(self):
        return None


class _Msal:
    def __init__(self, result: dict | None = None):
        self.result = result or {}

    def get_authorization_request_url(self, scopes, state, redirect_uri):
        return f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?state={state}"

    def acquire_token_by_authorization_code(self, code, scopes, redirect_uri):
        return self.result


def _patch_redis(monkeypatch, payload: bytes | None = None) -> _Redis:
    client = _Redis(payload)
    monkeypatch.setattr("app.api.auth.redis.Redis.from_url", lambda *args, **kwargs: client)
    return client


def _user_id(client, headers) -> str:
    return client.get("/users/me", headers=headers).json()["id"]


def _add_account(
    user_id: str,
    *,
    email: str = "ana@outlook.com",
    oid: str | None = None,
    provider: str = "microsoft",
    updated_at: datetime | None = None,
    disconnected_at: datetime | None = None,
    access_token: str = "secret-access-token",
) -> str:
    fields = {}
    if updated_at is not None:
        fields["updated_at"] = updated_at
    if disconnected_at is not None:
        fields["disconnected_at"] = disconnected_at
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
            provider=provider,
            provider_account_id=oid or f"oid-{uuid4()}",
            email=email,
            access_token=access_token,
            scopes="Mail.Read",
            delta_link="delta-link-secret",
            **fields,
        )
        session.add(account)
        session.commit()
        return str(account.id)


def _add_message(
    user_id: str,
    account_id: str,
    *,
    graph_id: str,
    received_at: datetime,
    priority_score: float | None,
    subject: str = "Hello",
    **columns,
) -> str:
    with Session(engine) as session:
        message = Message(
            user_id=UUID(user_id),
            connected_account_id=UUID(account_id),
            graph_id=graph_id,
            subject=subject,
            body_preview="preview",
            received_at=received_at,
            priority_score=priority_score,
            **columns,
        )
        session.add(message)
        session.commit()
        return str(message.id)


def test_frontend_url_defaults_to_the_vite_origin():
    assert Settings.model_fields["frontend_url"].default == "http://localhost:5173"


def test_microsoft_start_returns_an_authorization_url(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    token = headers["Authorization"].removeprefix("Bearer ")
    store = _patch_redis(monkeypatch)
    monkeypatch.setattr("app.api.auth.build_msal_app", lambda: _Msal())

    response = client.get("/auth/microsoft/start", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"authorization_url"}
    assert body["authorization_url"].startswith("https://login.microsoftonline.com/")
    assert token not in body["authorization_url"]
    assert store.saved is not None
    assert store.saved[2] == _user_id(client, headers)


def test_google_start_returns_an_authorization_url(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    token = headers["Authorization"].removeprefix("Bearer ")
    store = _patch_redis(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-client-secret")
    monkeypatch.setattr(
        settings,
        "google_redirect_uri",
        "http://localhost:8000/auth/google/callback",
    )

    missing = client.get("/auth/google/start")
    assert missing.status_code == 401

    response = client.get("/auth/google/start", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"authorization_url"}
    parts = urlsplit(body["authorization_url"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    query = parse_qs(parts.query)
    assert query["client_id"] == ["google-client-id"]
    assert query["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid email https://www.googleapis.com/auth/gmail.readonly"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert "client_secret" not in query
    assert token not in body["authorization_url"]
    assert "google-client-secret" not in body["authorization_url"]
    assert store.saved is not None
    key, ttl, value = store.saved
    assert key == f"oauth_state:{query['state'][0]}"
    assert ttl == 600
    assert value == _user_id(client, headers)


def test_google_start_without_config_does_not_store_state(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    store = _patch_redis(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:8000/auth/google/callback")

    response = client.get("/auth/google/start", headers=headers)

    assert response.status_code == 503
    assert response.json() == {"detail": "Gmail connect is temporarily unavailable."}
    assert "google-client" not in response.text
    assert store.saved is None


def test_profile_mailbox_is_null_until_a_mailbox_is_connected(client, register_user):
    headers = register_user("ana@example.com")
    me = client.get("/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["mailbox"] is None

    _add_account(_user_id(client, headers), email="ana@outlook.com", access_token="secret-access-token")
    connected_response = client.get("/users/me", headers=headers)
    connected = connected_response.json()
    assert connected["mailbox"] == {
        "connected": True,
        "provider": "microsoft",
        "email": "ana@outlook.com",
    }
    assert "secret-access-token" not in connected_response.text
    assert "delta-link-secret" not in connected_response.text
    assert "hashed_password" not in connected


def test_profile_hides_a_disconnected_mailbox_and_patch_keeps_the_active_one(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _add_account(
        user_id,
        email="old@outlook.com",
        updated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        disconnected_at=datetime(2024, 6, 2, tzinfo=timezone.utc),
    )
    _add_account(
        user_id,
        email="new@outlook.com",
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )

    assert client.get("/users/me", headers=headers).json()["mailbox"]["email"] == "new@outlook.com"
    patched = client.patch("/users/me", headers=headers, json={"display_name": "Ana"})
    assert patched.json()["display_name"] == "Ana"
    assert patched.json()["mailbox"]["email"] == "new@outlook.com"


def test_profile_mailbox_stays_outlook_when_gmail_was_updated_later(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _add_account(
        user_id,
        email="ana@outlook.com",
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    gmail_id = _add_account(
        user_id,
        email="ana@gmail.com",
        provider="google",
        updated_at=datetime(2024, 8, 1, tzinfo=timezone.utc),
    )
    with Session(engine) as session:
        gmail = session.get(ConnectedAccount, UUID(gmail_id))
        gmail.sync_status = "failed"
        gmail.sync_error_code = "gmail_unavailable"
        gmail.last_sync_at = datetime(2024, 7, 1, tzinfo=timezone.utc)
        session.add(gmail)
        session.commit()

    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"] == {
        "connected": True,
        "provider": "microsoft",
        "email": "ana@outlook.com",
    }
    assert me["sync"]["status"] == "failed"
    assert me["sync"]["error_code"] == "gmail_unavailable"
    by_provider = {item["provider"]: item for item in me["mailboxes"]}
    assert by_provider["microsoft"]["sync"] == {
        "status": "idle",
        "last_successful_at": None,
        "error_code": None,
    }
    assert by_provider["google"]["email"] == "ana@gmail.com"
    assert by_provider["google"]["sync"]["status"] == "failed"
    assert by_provider["google"]["sync"]["error_code"] == "gmail_unavailable"
    assert by_provider["google"]["sync"]["last_successful_at"] is not None


def test_sync_requires_a_mailbox_and_enqueues_the_latest_account(client, register_user):
    headers = register_user("ana@example.com")
    missing = client.post("/messages/sync", headers=headers)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "No mailbox is connected."}
    app.state.arq.enqueue_job.assert_not_awaited()

    user_id = _user_id(client, headers)
    _add_account(
        user_id,
        email="old@outlook.com",
        updated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        disconnected_at=datetime(2024, 6, 2, tzinfo=timezone.utc),
    )
    latest = _add_account(
        user_id,
        email="new@outlook.com",
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    response = client.post("/messages/sync", headers=headers)
    assert response.status_code == 202
    assert response.json()["enqueued"] is True
    assert response.json()["sync_status"] == "pending"
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", latest, 1)


def test_inbox_lists_gmail_and_outlook_together(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    outlook = _add_account(user_id, email="ana@outlook.com", oid="oid-ana")
    gmail = _add_account(user_id, email="ana@gmail.com", oid="sub-ana", provider="google")
    _add_message(
        user_id,
        outlook,
        graph_id="outlook-1",
        received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        priority_score=None,
        subject="From Outlook",
    )
    _add_message(
        user_id,
        gmail,
        graph_id="gmail-1",
        received_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        priority_score=None,
        subject="From Gmail",
    )

    listed = client.get("/messages", headers=headers)
    assert listed.status_code == 200
    assert listed.headers["X-Total-Count"] == "2"
    assert [(row["subject"], row["provider"]) for row in listed.json()] == [
        ("From Gmail", "google"),
        ("From Outlook", "microsoft"),
    ]
    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"] == {
        "connected": True,
        "provider": "microsoft",
        "email": "ana@outlook.com",
    }
    idle = {"status": "idle", "last_successful_at": None, "error_code": None}
    assert me["mailboxes"] == [
        {
            "connected": True,
            "provider": "google",
            "email": "ana@gmail.com",
            "sync": idle,
        },
        {
            "connected": True,
            "provider": "microsoft",
            "email": "ana@outlook.com",
            "sync": idle,
        },
    ]

    response = client.post("/messages/sync", headers=headers)
    assert response.status_code == 202
    assert response.json() == {"enqueued": True, "sync_status": "pending"}
    queued = {call.args[1] for call in app.state.arq.enqueue_job.await_args_list}
    assert queued == {outlook, gmail}

    monkeypatch.setattr("app.api.auth.revoke_google_token", lambda token: None)
    assert client.post("/auth/google/disconnect", headers=headers).status_code == 200
    remaining = client.get("/messages", headers=headers).json()
    assert [(row["subject"], row["provider"]) for row in remaining] == [("From Outlook", "microsoft")]
    assert client.get(f"/messages/{listed.json()[0]['id']}", headers=headers).status_code == 404
    still = client.get("/users/me", headers=headers).json()
    assert still["mailbox"]["provider"] == "microsoft"
    assert [item["provider"] for item in still["mailboxes"]] == ["microsoft"]


def test_message_list_contract_sort_and_total(client, register_user):
    ana = register_user("ana@example.com")
    bob = register_user("bob@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    rows = {
        "low": _add_message(
            ana_id,
            account_id,
            graph_id="low",
            received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            priority_score=10,
        ),
        "mid": _add_message(
            ana_id,
            account_id,
            graph_id="mid",
            received_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
            priority_score=50,
        ),
        "high": _add_message(
            ana_id,
            account_id,
            graph_id="high",
            received_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
            priority_score=50,
        ),
        "unscored": _add_message(
            ana_id,
            account_id,
            graph_id="unscored",
            received_at=datetime(2024, 12, 1, tzinfo=timezone.utc),
            priority_score=None,
            subject=None,
        ),
    }
    bob_account = _add_account(_user_id(client, bob), email="bob@outlook.com")
    _add_message(
        _user_id(client, bob),
        bob_account,
        graph_id="bob",
        received_at=datetime(2024, 12, 2, tzinfo=timezone.utc),
        priority_score=99,
    )

    empty = client.get("/messages", headers=register_user("cara@example.com"))
    assert empty.status_code == 200
    assert empty.json() == []
    assert empty.headers["X-Total-Count"] == "0"

    by_time = client.get("/messages", headers=ana)
    assert by_time.headers["X-Total-Count"] == "4"
    assert [item["id"] for item in by_time.json()] == [
        rows["unscored"],
        rows["high"],
        rows["mid"],
        rows["low"],
    ]
    explicit = client.get("/messages?sort=received_at", headers=ana)
    assert [item["id"] for item in explicit.json()] == [item["id"] for item in by_time.json()]

    by_priority = client.get("/messages?sort=priority&limit=1&offset=1", headers=ana)
    assert by_priority.headers["X-Total-Count"] == "4"
    assert [item["id"] for item in by_priority.json()] == [rows["mid"]]
    full_priority = client.get("/messages?sort=priority", headers=ana)
    assert [item["id"] for item in full_priority.json()] == [
        rows["high"],
        rows["mid"],
        rows["low"],
        rows["unscored"],
    ]
    origin = get_settings().cors_origin_list[0]
    exposed = client.get("/messages?sort=priority", headers={**ana, "Origin": origin})
    assert "x-total-count" in exposed.headers["access-control-expose-headers"].lower()

    item = by_time.json()[1]
    assert set(item) == LIST_KEYS
    assert item["subject"] == "Hello"
    assert item["body_preview"] == "preview"
    assert item["from_name"] is None
    assert item["from_email"] is None
    assert item["is_read"] is None
    assert item["conversation_id"] is None
    assert item["priority_score"] == 50
    assert item["priority_band"] is None
    assert item["priority_reasons"] == []
    assert item["category"] is None
    assert item["category_source"] is None
    assert item["category_confidence"] is None
    assert item["project_name"] is None
    assert item["project_number"] is None
    assert item["due_date"] is None
    assert item["responsible_party"] is None
    assert item["reference_number"] is None
    assert item["user_corrected"] is False
    assert "priority_scored_at" not in item
    assert item["suggested_form"] is None
    assert client.get("/messages?sort=nope", headers=ana).status_code == 422


def test_message_detail_includes_body_and_hides_other_users(client, register_user):
    ana = register_user("ana@example.com")
    bob = register_user("bob@example.com")
    ana_id = _user_id(client, ana)
    account_id = _add_account(ana_id)
    message_id = _add_message(
        ana_id,
        account_id,
        graph_id="one",
        received_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
        priority_score=None,
    )

    detail = client.get(f"/messages/{message_id}", headers=ana)
    assert detail.status_code == 200
    body = detail.json()
    assert set(body) == LIST_KEYS | {"body_text", "draft"}
    assert body["body_text"] is None
    assert body["draft"] is None
    assert body["priority_score"] is None
    assert body["priority_band"] is None
    assert body["priority_reasons"] == []
    assert body["category"] is None
    assert body["user_corrected"] is False
    assert "priority_scored_at" not in body

    missing = client.get(f"/messages/{uuid4()}", headers=ana)
    foreign = client.get(f"/messages/{message_id}", headers=bob)
    assert missing.status_code == foreign.status_code == 404
    assert missing.json() == foreign.json() == {"detail": "Message not found."}


def _callback(client, query: str):
    return client.get(f"/auth/microsoft/callback?{query}", follow_redirects=False)


def _query(location: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(location).query)


_OAUTH_STATE = "a" * 43


def test_callback_redirects_every_outcome(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    other = register_user("bob@example.com")
    _add_account(_user_id(client, other), email="taken@outlook.com", oid="oid-taken")

    _patch_redis(monkeypatch, None)
    denied = _callback(
        client,
        f"error=access_denied&error_description=The+user+cancelled&state={_OAUTH_STATE}",
    )
    assert denied.status_code == 302
    assert _query(denied.headers["location"]) == {"connected": ["0"], "error": ["microsoft_denied"]}
    assert "cancelled" not in denied.headers["location"]
    assert "access_denied" not in denied.headers["location"]

    missing = _callback(client, "")
    assert _query(missing.headers["location"])["error"] == ["invalid_state"]

    _patch_redis(monkeypatch, b"not-a-uuid")
    bad_state = _callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(bad_state.headers["location"])["error"] == ["invalid_state"]

    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal({"error": "invalid_grant", "error_description": "bad code"}),
    )
    exchanged = _callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(exchanged.headers["location"])["error"] == ["token_exchange"]
    assert "invalid_grant" not in exchanged.headers["location"]
    assert "bad code" not in exchanged.headers["location"]

    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "Mail.Read",
                "id_token_claims": {"oid": "oid-taken", "preferred_username": "taken@outlook.com"},
            }
        ),
    )
    taken = _callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(taken.headers["location"])["error"] == ["mailbox_taken"]
    app.state.arq.enqueue_job.assert_not_awaited()

    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "Mail.Read",
                "id_token_claims": {
                    "oid": "oid-ana",
                    "preferred_username": "ana@outlook.com",
                    "tid": "tenant-1",
                },
            }
        ),
    )
    success = _callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert success.status_code == 302
    location = success.headers["location"]
    base = get_settings().frontend_url.rstrip("/")
    assert location.startswith(f"{base}/inbox?")
    assert "email=ana%40outlook.com" in location
    assert _query(location) == {"connected": ["1"], "email": ["ana@outlook.com"]}
    assert client.get("/users/me", headers=headers).json()["mailbox"]["email"] == "ana@outlook.com"
    with Session(engine) as session:
        account = session.exec(
            select(ConnectedAccount).where(ConnectedAccount.provider_account_id == "oid-ana")
        ).one()
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", str(account.id), 1)


def _google_callback(client, query: str):
    return client.get(f"/auth/google/callback?{query}", follow_redirects=False)


def test_google_callback_redirects_every_outcome(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    other = register_user("bob@example.com")
    _add_account(
        _user_id(client, other),
        email="taken@gmail.com",
        oid="sub-taken",
        provider="google",
    )
    calls: list[str] = []

    def _exchange(code: str):
        calls.append(code)
        return _google_token

    _google_token = {
        "access_token": "google-access-token",
        "refresh_token": "google-refresh-token",
        "expires_in": 3600,
        "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
    }

    def _boom(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.api.auth.redis.Redis.from_url", _boom)
    monkeypatch.setattr("app.api.auth.exchange_authorization_code", _exchange)
    down = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(down.headers["location"])["error"] == ["invalid_state"]
    assert calls == []

    _patch_redis(monkeypatch, None)
    denied = _google_callback(
        client,
        f"error=access_denied&error_description=The+user+cancelled&state={_OAUTH_STATE}",
    )
    assert denied.status_code == 302
    assert _query(denied.headers["location"]) == {"connected": ["0"], "error": ["google_denied"]}
    assert "cancelled" not in denied.headers["location"]
    assert "access_denied" not in denied.headers["location"]

    missing = _google_callback(client, "")
    assert _query(missing.headers["location"])["error"] == ["invalid_state"]

    _patch_redis(monkeypatch, b"not-a-uuid")
    bad_state = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(bad_state.headers["location"])["error"] == ["invalid_state"]
    assert calls == []

    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr("app.api.auth.exchange_authorization_code", lambda code: None)
    exchanged = _google_callback(client, f"code=secret-code&state={_OAUTH_STATE}")
    assert _query(exchanged.headers["location"])["error"] == ["token_exchange"]
    assert "secret-code" not in exchanged.headers["location"]

    monkeypatch.setattr(
        "app.api.auth.exchange_authorization_code",
        lambda code: {
            "access_token": "google-access-token",
            "expires_in": 3600,
            "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
        },
    )
    no_refresh = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(no_refresh.headers["location"])["error"] == ["token_exchange"]

    monkeypatch.setattr("app.api.auth.exchange_authorization_code", lambda code: _google_token)
    monkeypatch.setattr("app.api.auth.fetch_mailbox_identity", lambda access_token: None)
    no_identity = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(no_identity.headers["location"])["error"] == ["token_exchange"]
    assert "google-access-token" not in no_identity.headers["location"]
    assert "google-refresh-token" not in no_identity.headers["location"]

    revoked: list[str] = []
    monkeypatch.setattr("app.api.auth.revoke_google_token", revoked.append)
    monkeypatch.setattr(
        "app.api.auth.exchange_authorization_code",
        lambda code: {**_google_token, "scope": "openid email"},
    )
    monkeypatch.setattr(
        "app.api.auth.fetch_mailbox_identity",
        lambda access_token: GoogleMailboxIdentity("sub-ana", "ana@gmail.com"),
    )
    _patch_redis(monkeypatch, user_id.encode())
    withheld = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(withheld.headers["location"]) == {"connected": ["0"], "error": ["google_denied"]}
    assert "openid" not in withheld.headers["location"]
    assert "google-refresh-token" not in withheld.headers["location"]
    assert revoked == ["google-refresh-token"]
    with Session(engine) as session:
        saved = session.exec(
            select(ConnectedAccount).where(ConnectedAccount.user_id == UUID(user_id))
        ).first()
        assert saved is None
    app.state.arq.enqueue_job.assert_not_awaited()
    monkeypatch.setattr("app.api.auth.exchange_authorization_code", lambda code: _google_token)

    monkeypatch.setattr(
        "app.api.auth.fetch_mailbox_identity",
        lambda access_token: GoogleMailboxIdentity("sub-taken", "taken@gmail.com"),
    )
    taken = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(taken.headers["location"])["error"] == ["mailbox_taken"]
    app.state.arq.enqueue_job.assert_not_awaited()

    _add_account(user_id, email="ana@outlook.com", oid="oid-ana")
    monkeypatch.setattr(
        "app.api.auth.fetch_mailbox_identity",
        lambda access_token: GoogleMailboxIdentity("sub-ana", "ana@gmail.com"),
    )
    both = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(both.headers["location"]) == {"connected": ["1"], "email": ["ana@gmail.com"]}
    together = client.get("/users/me", headers=headers).json()
    assert together["mailbox"]["provider"] == "microsoft"
    assert together["mailbox"]["email"] == "ana@outlook.com"
    assert {item["provider"] for item in together["mailboxes"]} == {"google", "microsoft"}
    assert revoked == ["google-refresh-token"]
    with Session(engine) as session:
        outlook = session.exec(
            select(ConnectedAccount).where(ConnectedAccount.provider_account_id == "oid-ana")
        ).one()
        assert outlook.disconnected_at is None
        assert outlook.access_token == "secret-access-token"
    app.state.arq.enqueue_job.reset_mock()

    with Session(engine) as session:
        outlook = session.exec(
            select(ConnectedAccount).where(ConnectedAccount.provider_account_id == "oid-ana")
        ).one()
        outlook.disconnected_at = datetime(2024, 6, 2, tzinfo=timezone.utc)
        session.add(outlook)
        session.commit()

    monkeypatch.setattr(
        "app.api.auth.fetch_mailbox_identity",
        lambda access_token: GoogleMailboxIdentity("sub-ana", "ana@gmail.com\nBcc: x"),
    )
    injected = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert _query(injected.headers["location"])["error"] == ["token_exchange"]
    assert "\n" not in injected.headers["location"]
    assert "Bcc" not in injected.headers["location"]

    monkeypatch.setattr(
        "app.api.auth.fetch_mailbox_identity",
        lambda access_token: GoogleMailboxIdentity("sub-ana", "ana@gmail.com"),
    )
    success = _google_callback(client, f"code=abc&state={_OAUTH_STATE}")
    assert success.status_code == 302
    location = success.headers["location"]
    base = get_settings().frontend_url.rstrip("/")
    assert location.startswith(f"{base}/inbox?")
    assert _query(location) == {"connected": ["1"], "email": ["ana@gmail.com"]}
    assert "google-access-token" not in location
    assert "google-refresh-token" not in location
    me = client.get("/users/me", headers=headers).json()
    assert me["mailbox"] == {
        "connected": True,
        "provider": "google",
        "email": "ana@gmail.com",
    }
    assert "google-refresh-token" not in client.get("/users/me", headers=headers).text
    with Session(engine) as session:
        account = session.exec(
            select(ConnectedAccount).where(ConnectedAccount.provider_account_id == "sub-ana")
        ).one()
        assert account.provider == "google"
        assert account.refresh_token == "google-refresh-token"
        assert account.tenant_id is None
        raw = session.execute(
            text("SELECT access_token, refresh_token FROM connected_accounts WHERE id = :id"),
            {"id": account.id},
        ).one()
    assert "google-access-token" not in raw[0]
    assert "google-refresh-token" not in raw[1]
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", str(account.id), 2)
