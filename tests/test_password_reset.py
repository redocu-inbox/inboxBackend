"""Password reset: no account oracle, single-use codes, and session_version revocation."""

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import jwt
import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.main import app
from app.models.password_reset import PasswordResetToken
from app.models.user import User, utcnow
from app.services.auth.password_reset import SUBJECT, consume_reset_token
from app.services.email.base import EmailDeliveryError
from app.services.email.fake import FAKE_SENDER
from app.worker.tasks import send_password_reset

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "replacement-password"


class _Redis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def eval(self, script, numkeys, key, window):
        del script, numkeys, window
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def rate_limit_store(monkeypatch):
    store = _Redis()
    monkeypatch.setattr("app.services.auth.rate_limit._client", lambda: store)
    FAKE_SENDER.clear()
    return store


def _deliver_enqueued() -> None:
    args = app.state.arq.enqueue_job.await_args.args
    assert args[0] == "send_password_reset"
    assert len(args) == 3
    joined = " ".join(args)
    assert "token=" not in joined
    asyncio.run(send_password_reset({}, args[1], args[2]))


def _token_from_outbox() -> str:
    assert len(FAKE_SENDER.messages) == 1
    message = FAKE_SENDER.messages[0]
    assert message.subject == SUBJECT
    assert message.text.startswith(
        "Someone asked to reset the password for your redocu account. "
        "Use this link within 30 minutes: "
    )
    assert message.text.endswith(
        ". If it wasn't you, ignore this email; your password hasn't changed."
    )
    link = message.text.split("minutes: ", 1)[1].split(". If it wasn't you", 1)[0]
    parts = urlsplit(link)
    assert parts.query == ""
    assert parts.fragment.startswith("token=")
    return parse_qs(parts.fragment)["token"][0]


def test_forgot_password_is_the_same_for_known_and_unknown_email(client, register_user, caplog):
    register_user("ana@example.com")
    known = client.post("/auth/forgot-password", json={"email": "Ana@Example.com"})
    missing = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == missing.status_code == 202
    assert known.json() == missing.json() == {}
    assert app.state.arq.enqueue_job.await_count == 1
    _deliver_enqueued()
    assert FAKE_SENDER.messages[0].to == "ana@example.com"
    assert _token_from_outbox() not in caplog.text
    assert "ana@example.com" not in caplog.text


def test_invalid_email_is_422_and_does_not_consume_the_limiter(client, register_user):
    register_user("ana@example.com")
    for _ in range(5):
        assert client.post("/auth/forgot-password", json={"email": "not-an-email"}).status_code == 422
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202


def test_email_and_ip_limits_do_not_reveal_the_account(client, register_user, monkeypatch):
    register_user("ana@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "forwarded_for_hops", 1)
    for index in range(5):
        response = client.post(
            "/auth/forgot-password",
            json={"email": "Ana@Example.com"},
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
        )
        assert response.status_code == 202
    limited = client.post(
        "/auth/forgot-password",
        json={"email": "ana@example.com"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    unknown = client.post(
        "/auth/forgot-password",
        json={"email": "nobody@example.com"},
        headers={"X-Forwarded-For": "198.51.100.8"},
    )
    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limited"}
    assert unknown.status_code == 202

    for index in range(5):
        response = client.post(
            "/auth/forgot-password",
            json={"email": f"person{index}@example.com"},
            headers={"X-Forwarded-For": "198.51.100.20"},
        )
        assert response.status_code == 202
    same_ip = client.post(
        "/auth/forgot-password",
        json={"email": "fresh@example.com"},
        headers={"X-Forwarded-For": "198.51.100.20"},
    )
    assert same_ip.status_code == 429
    assert same_ip.json() == {"detail": "rate_limited"}


def test_forwarded_for_is_ignored_unless_configured(client, register_user):
    register_user("ana@example.com")
    for index in range(5):
        response = client.post(
            "/auth/forgot-password",
            json={"email": f"user{index}@example.com"},
            headers={"X-Forwarded-For": f"198.51.100.{index}"},
        )
        assert response.status_code == 202
    spoofed = client.post(
        "/auth/forgot-password",
        json={"email": "another@example.com"},
        headers={"X-Forwarded-For": "203.0.113.50"},
    )
    assert spoofed.status_code == 429


def test_rate_limit_store_down_is_the_same_503(client, register_user, monkeypatch):
    register_user("ana@example.com")

    def unavailable():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.services.auth.rate_limit._client", unavailable)
    known = client.post("/auth/forgot-password", json={"email": "ana@example.com"})
    missing = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == missing.status_code == 503
    assert known.json() == missing.json() == {"detail": "password_reset_unavailable"}
    assert "redis down" not in known.text
    with Session(engine) as session:
        assert session.exec(select(PasswordResetToken)).first() is None


def test_reset_replaces_the_password_and_rejects_every_older_token(client, register_user):
    headers = register_user("ana@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    now = utcnow()
    legacy = jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(hours=1)},
        get_settings().secret_key,
        algorithm="HS256",
    )
    same_second = jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": "6f1b1b1b-6f1b-4b1b-8f1b-6f1b1b1b1b1b",
            "ver": 0,
        },
        get_settings().secret_key,
        algorithm="HS256",
    )
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    _deliver_enqueued()
    token = _token_from_outbox()
    with Session(engine) as session:
        stored = session.exec(select(PasswordResetToken)).one()
        assert stored.token_hash != token
        assert token not in stored.token_hash
    reset = client.post("/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert reset.status_code == 204
    assert reset.content == b""
    assert client.get("/users/me", headers=headers).status_code == 401
    assert client.get("/users/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 401
    assert client.get("/users/me", headers={"Authorization": f"Bearer {same_second}"}).status_code == 401
    old_login = client.post(
        "/auth/login", json={"email": "ana@example.com", "password": PASSWORD}
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/auth/login", json={"email": "ana@example.com", "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 200
    assert client.get(
        "/users/me",
        headers={"Authorization": f"Bearer {new_login.json()['access_token']}"},
    ).status_code == 200
    again = client.post("/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert again.status_code == 400
    assert again.json() == {"detail": "reset_token_invalid"}


def test_logout_still_revokes_only_the_calling_token(client, register_user):
    first = register_user("ana@example.com")
    second = client.post("/auth/login", json={"email": "ana@example.com", "password": PASSWORD})
    second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}
    assert client.post("/auth/logout", headers=first).status_code == 204
    assert client.get("/users/me", headers=first).status_code == 401
    assert client.get("/users/me", headers=second_headers).status_code == 200


def test_unknown_expired_and_bad_passwords(client, register_user):
    register_user("ana@example.com")
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    _deliver_enqueued()
    token = _token_from_outbox()
    with Session(engine) as session:
        row = session.exec(select(PasswordResetToken)).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()
    expired = client.post(
        "/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}
    )
    unknown = client.post(
        "/auth/reset-password", json={"token": "a" * 43, "password": NEW_PASSWORD}
    )
    assert expired.status_code == unknown.status_code == 400
    assert expired.json() == unknown.json() == {"detail": "reset_token_invalid"}
    assert client.post(
        "/auth/reset-password", json={"token": token, "password": "short"}
    ).status_code == 422
    assert client.post(
        "/auth/reset-password", json={"token": token, "password": "x" * 129}
    ).status_code == 422


def test_newer_request_invalidates_the_older_code(client, register_user):
    register_user("ana@example.com")
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    first_args = app.state.arq.enqueue_job.await_args.args
    asyncio.run(send_password_reset({}, first_args[1], first_args[2]))
    first = _token_from_outbox()
    FAKE_SENDER.clear()
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    asyncio.run(send_password_reset({}, first_args[1], first_args[2]))
    assert FAKE_SENDER.messages == []
    second_args = app.state.arq.enqueue_job.await_args.args
    asyncio.run(send_password_reset({}, second_args[1], second_args[2]))
    second = _token_from_outbox()
    assert client.post(
        "/auth/reset-password", json={"token": first, "password": NEW_PASSWORD}
    ).status_code == 400
    assert client.post(
        "/auth/reset-password", json={"token": second, "password": NEW_PASSWORD}
    ).status_code == 204


def test_delivery_failure_does_not_leak_the_code(client, register_user, monkeypatch):
    register_user("ana@example.com")
    assert client.post("/auth/forgot-password", json={"email": "ana@example.com"}).status_code == 202
    args = app.state.arq.enqueue_job.await_args.args

    class Boom:
        def send(self, message):
            raise EmailDeliveryError("status=503")

    monkeypatch.setattr("app.services.auth.password_reset.get_email_sender", lambda: Boom())
    with pytest.raises(EmailDeliveryError) as caught:
        asyncio.run(send_password_reset({}, args[1], args[2]))
    assert str(caught.value) == "status=503"
    assert "token" not in str(caught.value)
    monkeypatch.setattr(
        "app.services.auth.password_reset.get_email_sender",
        lambda: FAKE_SENDER,
    )
    asyncio.run(send_password_reset({}, args[1], args[2]))
    token = _token_from_outbox()
    with Session(engine) as session:
        rows = session.exec(select(PasswordResetToken)).all()
        assert len(rows) == 2
        assert all(row.token_hash != token for row in rows)
        assert sum(row.used_at is None for row in rows) == 1


def test_concurrent_reset_consumes_the_code_once():
    from tests.conftest import _TEST_DATABASE  # noqa: F401

    with Session(engine) as session:
        user = User(email="race@example.com", hashed_password="x")
        session.add(user)
        session.commit()
        user_id = user.id
    from app.services.auth.password_reset import open_reset_request, deliver_password_reset

    with Session(engine) as session:
        request_id = open_reset_request(session, session.get(User, user_id))
    with Session(engine) as session:
        deliver_password_reset(session, user_id, request_id)
    token = _token_from_outbox()
    results: list[bool] = []

    def attempt() -> None:
        with Session(engine) as session:
            results.append(consume_reset_token(session, token, "concurrent-password"))

    import threading

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def _cors_middleware():
    current = app.middleware_stack
    while current is not None:
        if current.__class__.__name__ == "CORSMiddleware":
            return current
        current = getattr(current, "app", None)
    raise AssertionError("CORS middleware was not installed")


def test_redocu_preflight_and_total_count_header(client, register_user):
    register_user("ana@example.com")
    cors = _cors_middleware()
    previous = list(cors.allow_origins)
    cors.allow_origins = [*previous, "https://redocu.app"]
    try:
        preflight = client.options(
            "/users/me",
            headers={
                "Origin": "https://redocu.app",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type,accept",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://redocu.app"
        allowed_headers = preflight.headers["access-control-allow-headers"].lower()
        assert "authorization" in allowed_headers
        assert "content-type" in allowed_headers
        assert "accept" in allowed_headers
        allowed_methods = preflight.headers["access-control-allow-methods"].upper()
        assert "GET" in allowed_methods
        assert "POST" in allowed_methods
        assert "PATCH" in allowed_methods
        assert "OPTIONS" in allowed_methods
        assert preflight.headers.get("access-control-allow-credentials") is None
        listed = client.get("/messages", headers={"Origin": "https://redocu.app"})
        assert "x-total-count" in listed.headers["access-control-expose-headers"].lower()
        blocked = client.options(
            "/users/me",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert blocked.status_code == 400
        assert blocked.headers.get("access-control-allow-origin") is None
    finally:
        cors.allow_origins = previous
