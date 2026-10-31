"""Microsoft tokens must be unreadable in the database and usable in code."""

from uuid import uuid4

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import text
from sqlmodel import Session

from app import security
from app.db import engine
from app.models.connected_account import ConnectedAccount
from app.models.user import User
from app.services.graph.tokens import get_account, save_tokens

ACCESS = "plaintext-access-token"
REFRESH = "plaintext-refresh-token"


def _raw_tokens(account_id) -> tuple[str, str | None]:
    """Read the stored bytes directly, bypassing the column type."""
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT access_token, refresh_token FROM connected_accounts WHERE id = :id"),
            {"id": account_id},
        ).one()
    return row.access_token, row.refresh_token


@pytest.fixture
def account_id():
    with Session(engine) as session:
        user = User(email=f"{uuid4()}@example.com")
        session.add(user)
        session.flush()
        account = ConnectedAccount(
            user_id=user.id,
            provider_account_id="oid",
            email="mailbox@example.com",
            access_token="initial",
        )
        session.add(account)
        session.commit()
        return account.id


def test_save_tokens_stores_ciphertext_and_reads_back_plaintext(account_id):
    with Session(engine) as session:
        save_tokens(
            session,
            get_account(session, account_id),
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_at=None,
            scopes="Mail.Read",
        )

    raw_access, raw_refresh = _raw_tokens(account_id)
    assert ACCESS not in raw_access
    assert REFRESH not in raw_refresh

    with Session(engine) as session:
        account = get_account(session, account_id)
        assert account.access_token == ACCESS
        assert account.refresh_token == REFRESH


def test_null_refresh_token_stays_null(account_id):
    assert _raw_tokens(account_id)[1] is None
    with Session(engine) as session:
        assert get_account(session, account_id).refresh_token is None


def test_tokens_do_not_appear_in_repr(account_id):
    with Session(engine) as session:
        account = get_account(session, account_id)
        save_tokens(
            session,
            account,
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_at=None,
            scopes=None,
        )
        session.refresh(account)
        assert ACCESS not in repr(account)
        assert REFRESH not in repr(account)


def test_tampered_ciphertext_fails_loudly(account_id):
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE connected_accounts SET access_token = 'tampered' WHERE id = :id"),
            {"id": account_id},
        )
    with Session(engine) as session:
        with pytest.raises(InvalidToken):
            get_account(session, account_id)


def test_missing_key_gives_an_actionable_error(monkeypatch):
    settings = security.get_settings()
    monkeypatch.setattr(settings, "token_encryption_key", "")
    security._get_fernet.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
            security.EncryptedText().process_bind_param("secret", None)
    finally:
        monkeypatch.undo()
        security._get_fernet.cache_clear()
