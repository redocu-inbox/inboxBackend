"""Password hashing, login JWTs, and encryption of stored OAuth tokens."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

_JWT_ALGORITHM = "HS256"

_hasher = PasswordHasher()
# Verified against when the email is unknown, so a failed login takes the same
# time whether or not the account exists.
_DUMMY_HASH = _hasher.hash("no account has this password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    """Check a password. `hashed` is None when the email is unknown or has no password."""
    try:
        return _hasher.verify(hashed or _DUMMY_HASH, password) and hashed is not None
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True)
class AccessTokenClaims:
    """Verified login JWT. `jti` is None for tokens issued before revocation existed."""

    user_id: UUID
    jti: UUID | None
    issued_at: datetime
    expires_at: datetime


def _as_utc(value: datetime | int | float) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(value, timezone.utc)


def create_access_token(user_id: UUID) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    claims = {"sub": str(user_id), "iat": now, "exp": expires_at, "jti": str(uuid4())}
    return jwt.encode(claims, settings.secret_key, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> AccessTokenClaims | None:
    """Return claims from a valid token, or None if it is invalid or expired.

    `jti` is optional so tokens issued before this change still work until they expire.
    A present `jti` that is not a UUID is rejected.
    """
    try:
        claims = jwt.decode(
            token,
            get_settings().secret_key,
            # Pinned so a token cannot choose its own algorithm, such as `none`.
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
        jti_raw = claims.get("jti")
        jti = UUID(str(jti_raw)) if jti_raw is not None else None
        return AccessTokenClaims(
            user_id=UUID(claims["sub"]),
            jti=jti,
            issued_at=_as_utc(claims["iat"]),
            expires_at=_as_utc(claims["exp"]),
        )
    except (jwt.InvalidTokenError, ValueError):
        return None


@lru_cache
def _get_fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key)


class EncryptedText(TypeDecorator):
    """A text column stored encrypted. Python code only ever sees the plaintext."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _get_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _get_fernet().decrypt(value.encode()).decode()
