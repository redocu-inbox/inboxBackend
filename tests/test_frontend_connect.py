"""Milestone F: mailbox on the profile, sync, message contract, and the OAuth redirect."""

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from sqlmodel import Session, select

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
    "conversation_id",
    "priority_score",
    "priority_band",
    "priority_reasons",
    "suggested_form",
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
    updated_at: datetime | None = None,
    access_token: str = "secret-access-token",
) -> str:
    fields = {}
    if updated_at is not None:
        fields["updated_at"] = updated_at
    with Session(engine) as session:
        account = ConnectedAccount(
            user_id=UUID(user_id),
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


def test_profile_uses_the_latest_mailbox_and_patch_keeps_it(client, register_user):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    _add_account(
        user_id,
        email="old@outlook.com",
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    _add_account(
        user_id,
        email="new@outlook.com",
        updated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )

    assert client.get("/users/me", headers=headers).json()["mailbox"]["email"] == "new@outlook.com"
    patched = client.patch("/users/me", headers=headers, json={"display_name": "Ana"})
    assert patched.json()["display_name"] == "Ana"
    assert patched.json()["mailbox"]["email"] == "new@outlook.com"


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
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    latest = _add_account(
        user_id,
        email="new@outlook.com",
        updated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    response = client.post("/messages/sync", headers=headers)
    assert response.status_code == 202
    assert response.json() == {"enqueued": True}
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", latest)


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
    assert body["priority_reasons"] == []

    missing = client.get(f"/messages/{uuid4()}", headers=ana)
    foreign = client.get(f"/messages/{message_id}", headers=bob)
    assert missing.status_code == foreign.status_code == 404
    assert missing.json() == foreign.json() == {"detail": "Message not found."}


def _callback(client, query: str):
    return client.get(f"/auth/microsoft/callback?{query}", follow_redirects=False)


def _query(location: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(location).query)


def test_callback_redirects_every_outcome(client, register_user, monkeypatch):
    headers = register_user("ana@example.com")
    user_id = _user_id(client, headers)
    other = register_user("bob@example.com")
    _add_account(_user_id(client, other), email="taken@outlook.com", oid="oid-taken")

    _patch_redis(monkeypatch, None)
    denied = _callback(
        client,
        "error=access_denied&error_description=The+user+cancelled&state=denied-state",
    )
    assert denied.status_code == 302
    assert _query(denied.headers["location"]) == {"connected": ["0"], "error": ["microsoft_denied"]}
    assert "cancelled" not in denied.headers["location"]
    assert "access_denied" not in denied.headers["location"]

    missing = _callback(client, "")
    assert _query(missing.headers["location"])["error"] == ["invalid_state"]

    _patch_redis(monkeypatch, b"not-a-uuid")
    bad_state = _callback(client, "code=abc&state=nope")
    assert _query(bad_state.headers["location"])["error"] == ["invalid_state"]

    _patch_redis(monkeypatch, user_id.encode())
    monkeypatch.setattr(
        "app.api.auth.build_msal_app",
        lambda: _Msal({"error": "invalid_grant", "error_description": "bad code"}),
    )
    exchanged = _callback(client, "code=abc&state=ok")
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
    taken = _callback(client, "code=abc&state=ok")
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
    success = _callback(client, "code=abc&state=ok")
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
    app.state.arq.enqueue_job.assert_awaited_once_with("sync_mailbox_delta", str(account.id))
