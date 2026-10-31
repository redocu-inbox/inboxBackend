from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db import engine
from app.models.user import User
from app.security import create_access_token
from sqlmodel import Session, select

PASSWORD = "correct-horse-battery"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _signed_token(sub: str, *, key: str | None = None, algorithm: str = "HS256", **claims) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": sub, "iat": now, "exp": now + timedelta(hours=1), **claims}
    return jwt.encode(payload, key, algorithm=algorithm)


def test_register_then_read_me(client):
    response = client.post(
        "/auth/register",
        json={"email": "ana@example.com", "password": PASSWORD, "display_name": "Ana"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_expire_minutes * 60

    me = client.get("/users/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "ana@example.com"
    assert me.json()["display_name"] == "Ana"
    assert "hashed_password" not in me.json()


def test_password_is_hashed_in_the_database(client, register_user):
    register_user("ana@example.com")
    with Session(engine) as session:
        user = session.exec(select(User)).one()
    assert user.hashed_password.startswith("$argon2")
    assert PASSWORD not in user.hashed_password
    assert user.hashed_password not in repr(user)


def test_duplicate_email_conflicts_regardless_of_case(client, register_user):
    register_user("ana@example.com")
    response = client.post(
        "/auth/register", json={"email": "ANA@example.com", "password": PASSWORD}
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "ana@example.com", "password": "short"},
        {"email": "ana@example.com", "password": "x" * 129},
    ],
)
def test_register_rejects_invalid_input(client, payload):
    assert client.post("/auth/register", json=payload).status_code == 422


def test_login_ignores_email_case(client, register_user):
    register_user("ana@example.com")
    response = client.post("/auth/login", json={"email": "Ana@Example.com", "password": PASSWORD})
    assert response.status_code == 200
    assert client.get("/users/me", headers=_bearer(response.json()["access_token"])).status_code == 200


def test_login_failures_are_indistinguishable(client, register_user):
    register_user("ana@example.com")
    wrong_password = client.post(
        "/auth/login", json={"email": "ana@example.com", "password": "wrong-password"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_account_without_a_password_cannot_log_in(client):
    with Session(engine) as session:
        session.add(User(email="legacy@example.com"))
        session.commit()
    response = client.post(
        "/auth/login", json={"email": "legacy@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_missing_token_is_401_with_challenge(client):
    response = client.get("/users/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_old_user_id_header_no_longer_authenticates(client, register_user):
    headers = register_user("ana@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    assert client.get("/users/me", headers={"X-User-Id": user_id}).status_code == 401


@pytest.mark.parametrize(
    "make_token",
    [
        lambda sub: "garbage",
        lambda sub: _signed_token(sub, key="another-secret-key-that-is-also-long-enough"),
        lambda sub: _signed_token(
            sub,
            key=get_settings().secret_key,
            exp=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
        lambda sub: _signed_token("not-a-uuid", key=get_settings().secret_key),
        lambda sub: _signed_token(sub, key=None, algorithm="none"),
    ],
    ids=["garbage", "wrong-key", "expired", "bad-subject", "alg-none"],
)
def test_bad_tokens_are_rejected(client, register_user, make_token):
    # The tokens name a real user, so only token validation can reject them.
    headers = register_user("ana@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    assert client.get("/users/me", headers=_bearer(make_token(user_id))).status_code == 401


def test_token_without_expiry_is_rejected(client, register_user):
    headers = register_user("ana@example.com")
    user_id = client.get("/users/me", headers=headers).json()["id"]
    token = jwt.encode(
        {"sub": user_id, "iat": datetime.now(timezone.utc)},
        get_settings().secret_key,
        algorithm="HS256",
    )
    assert client.get("/users/me", headers=_bearer(token)).status_code == 401


def test_token_for_a_deleted_user_is_rejected(client):
    assert client.get("/users/me", headers=_bearer(create_access_token(uuid4()))).status_code == 401


def test_update_profile(client, register_user):
    headers = register_user("ana@example.com")
    before = client.get("/users/me", headers=headers).json()

    updated = client.patch("/users/me", headers=headers, json={"display_name": "Ana M"})
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Ana M"
    assert updated.json()["updated_at"] > before["updated_at"]

    # An empty body changes nothing; an explicit null clears the name.
    assert client.patch("/users/me", headers=headers, json={}).json()["display_name"] == "Ana M"
    assert (
        client.patch("/users/me", headers=headers, json={"display_name": None}).json()["display_name"]
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "evil@example.com"},
        {"hashed_password": "x"},
        {"display_name": "x" * 257},
    ],
)
def test_update_profile_rejects_other_fields(client, register_user, payload):
    headers = register_user("ana@example.com")
    assert client.patch("/users/me", headers=headers, json=payload).status_code == 422
    assert client.get("/users/me", headers=headers).json()["email"] == "ana@example.com"


def test_update_profile_requires_auth(client):
    assert client.patch("/users/me", json={"display_name": "x"}).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_key": "change-me"},
        {"secret_key": "change-me-to-a-long-random-string"},
        {"secret_key": "too-short"},
        {"token_encryption_key": ""},
    ],
    ids=["default", "example", "short", "no-encryption-key"],
)
def test_production_refuses_weak_secrets(overrides):
    values = {
        "app_env": "production",
        "secret_key": "x" * 32,
        "token_encryption_key": get_settings().token_encryption_key,
        **overrides,
    }
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_malformed_encryption_key_is_refused_everywhere():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="development", token_encryption_key="not-a-fernet-key")


def test_development_allows_placeholder_secrets():
    Settings(_env_file=None, app_env="development", secret_key="change-me", token_encryption_key="")
