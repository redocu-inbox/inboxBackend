"""Registration, login, logout, and mailbox connect.

Register and login are plain `def` routes because password hashing is deliberately
slow. FastAPI runs them in a thread pool, so hashing does not block the event loop.
"""

from datetime import timedelta
import logging
import re
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import AfterValidator, BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
import redis
from sqlmodel import Session, select
from typing import Annotated
from urllib.parse import urlencode

from app.api.deps import _bearer, get_arq, get_current_user, get_session
from app.config import get_settings
from app.models.revoked_token import RevokedToken
from app.models.user import User, utcnow
from app.security import create_access_token, decode_access_token, hash_password, verify_password
from app.services.auth.password_reset import consume_reset_token, open_reset_request
from app.services.auth.rate_limit import (
    RateLimitExceeded,
    RateLimitUnavailable,
    client_ip,
    enforce_password_reset_limit,
)
from app.services.google.connect import connect_google_mailbox
from app.services.google.http import fetch_mailbox_identity
from app.services.google.oauth import (
    exchange_authorization_code,
    google_authorization_url,
    revoke_google_token,
)
from app.services.graph.connect import ConnectStatus, connect_microsoft_mailbox
from app.services.graph.oauth import build_msal_app
from app.services.mailboxes import (
    disconnect_active_mailbox,
    lock_active_mailbox,
    mark_sync_enqueue_failed,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Stored and looked up in lowercase, so Ana@x.com and ana@x.com are one account.
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]

# token_urlsafe(32) is 43 characters from this alphabet. Reject anything else
# before it is used as a Redis key.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_STATE_TTL_SECONDS = 600


class RegisterRequest(BaseModel):
    email: NormalizedEmail
    # The upper limit stops very long passwords from tying up the hasher.
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)


class LoginRequest(BaseModel):
    email: NormalizedEmail
    password: str = Field(max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: NormalizedEmail


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MicrosoftStartResponse(BaseModel):
    authorization_url: str


class DisconnectResponse(BaseModel):
    disconnected: bool


# The frontend branches on these. Provider text is logged and stays out of the URL.
_ERR_DENIED = "microsoft_denied"
_ERR_GOOGLE_DENIED = "google_denied"
_ERR_STATE = "invalid_state"
_ERR_TOKEN = "token_exchange"
_ERR_TAKEN = "mailbox_taken"
_ERR_CONFLICT = "mailbox_conflict"

_UNAVAILABLE = object()


def _token_response(session: Session, user: User) -> TokenResponse:
    """Mint a JWT for the session_version currently stored on the user row."""
    locked = session.exec(select(User).where(User.id == user.id).with_for_update()).one()
    response = TokenResponse(
        access_token=create_access_token(locked.id, int(locked.session_version or 0)),
        expires_in=get_settings().access_token_expire_minutes * 60,
    )
    session.commit()
    return response


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, session: Session = Depends(get_session)) -> TokenResponse:
    user = User(
        email=body.email,
        display_name=body.display_name,
        hashed_password=hash_password(body.password),
    )
    session.add(user)
    try:
        # Relies on the unique index, so two simultaneous sign-ups cannot both succeed.
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from exc
    session.refresh(user)
    return _token_response(session, user)


@router.post("/login")
def login(body: LoginRequest, session: Session = Depends(get_session)) -> TokenResponse:
    user = session.exec(select(User).where(User.email == body.email)).first()
    if not verify_password(body.password, user.hashed_password if user else None):
        # Same response for a wrong password and an unknown email.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token_response(session, user)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Accept a reset request without saying whether the email is registered.

    The response is always 202 and an empty object after the rate limit passes.
    Delivery happens in the worker, which is the only place that creates the code.
    """
    try:
        enforce_password_reset_limit(email=body.email, ip=client_ip(request))
    except RateLimitUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="password_reset_unavailable",
        ) from None
    except RateLimitExceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
        ) from None
    user = session.exec(select(User).where(User.email == body.email)).first()
    if user is None:
        return {}
    request_id = open_reset_request(session, user)
    try:
        await get_arq(request).enqueue_job("send_password_reset", str(user.id), str(request_id))
    except Exception:
        logger.warning("password reset enqueue failed")
    return {}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    body: ResetPasswordRequest,
    session: Session = Depends(get_session),
) -> Response:
    if not consume_reset_token(session, body.token, body.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reset_token_invalid",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Revoke the caller's current app JWT. Other jti tokens stay valid.

    A legacy token with no jti sets users.auth_valid_after to that token's iat,
    which rejects other legacy tokens issued at or before that instant. Tokens
    that have a jti are not affected by auth_valid_after.
    """
    claims = decode_access_token(credentials.credentials) if credentials else None
    if claims is None or claims.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if claims.jti is not None:
        session.add(
            RevokedToken(
                jti=claims.jti,
                user_id=user.id,
                expires_at=claims.expires_at,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
    else:
        if user.auth_valid_after is None or claims.issued_at > user.auth_valid_after:
            user.auth_valid_after = claims.issued_at
            user.updated_at = utcnow()
            session.add(user)
            session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _frontend_origin() -> str:
    """Origin from server config. Never taken from the request."""
    return get_settings().frontend_url.rstrip("/")


def _inbox_redirect(**query: str) -> RedirectResponse:
    """302 to the configured frontend origin. Query values are encoded once."""
    url = f"{_frontend_origin()}/inbox?{urlencode(query)}"
    response = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    # RedirectResponse quotes the location. Set the already-encoded URL so
    # the email is not encoded a second time.
    response.headers["location"] = url
    return response


def _inbox_failure(code: str, *, provider: str = "Microsoft") -> RedirectResponse:
    logger.warning("%s sign-in failed (%s)", provider, code)
    return _inbox_redirect(connected="0", error=code)


def _redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _consume_oauth_user_id(state: str) -> UUID | None | object:
    """Read and delete the one-time state.

    None means the state is missing, malformed, or unknown.
    `_UNAVAILABLE` means Redis could not be used. The code must not be exchanged.
    """
    if not _STATE_RE.fullmatch(state):
        return None
    try:
        client = _redis_client()
        try:
            raw = client.getdel(f"oauth_state:{state}")
        finally:
            client.close()
    except Exception:
        logger.warning("oauth state store unavailable")
        return _UNAVAILABLE
    if not raw:
        return None
    try:
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        return UUID(text)
    except (UnicodeDecodeError, ValueError):
        return None


def _store_oauth_state(state: str, user_id: UUID) -> None:
    client = _redis_client()
    try:
        client.setex(f"oauth_state:{state}", _STATE_TTL_SECONDS, str(user_id))
    finally:
        client.close()


def _delete_oauth_state(state: str) -> None:
    try:
        client = _redis_client()
        try:
            client.delete(f"oauth_state:{state}")
        finally:
            client.close()
    except Exception:
        logger.warning("oauth state cleanup failed")


@router.get("/microsoft/start", status_code=status.HTTP_200_OK)
def microsoft_start(user: User = Depends(get_current_user)) -> MicrosoftStartResponse:
    """Return Microsoft's authorize URL. The Bearer token is not copied into it."""
    state = secrets.token_urlsafe(32)
    try:
        _store_oauth_state(state, user.id)
    except Exception as exc:
        logger.warning("oauth state store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outlook connect is temporarily unavailable.",
        ) from exc
    settings = get_settings()
    try:
        url = build_msal_app().get_authorization_request_url(
            settings.graph_scope_list,
            state=state,
            redirect_uri=settings.graph_redirect_uri,
        )
    except Exception as exc:
        _delete_oauth_state(state)
        logger.warning("Microsoft authorize URL failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outlook connect is temporarily unavailable.",
        ) from exc
    return MicrosoftStartResponse(authorization_url=url)


def _exchange_code(code: str) -> dict | None:
    settings = get_settings()
    try:
        result = build_msal_app().acquire_token_by_authorization_code(
            code,
            scopes=settings.graph_scope_list,
            redirect_uri=settings.graph_redirect_uri,
        )
    except Exception as exc:
        logger.warning("Microsoft token exchange failed error_type=%s", type(exc).__name__)
        return None
    if not isinstance(result, dict):
        return None
    return result


@router.get("/google/start", status_code=status.HTTP_200_OK)
def google_start(user: User = Depends(get_current_user)) -> MicrosoftStartResponse:
    """Return Google's authorize URL. The Bearer token is not copied into it."""
    settings = get_settings()
    if not settings.google_client_id or not settings.google_redirect_uri:
        logger.warning("Google authorize URL is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail connect is temporarily unavailable.",
        )
    state = secrets.token_urlsafe(32)
    try:
        _store_oauth_state(state, user.id)
    except Exception as exc:
        logger.warning("oauth state store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail connect is temporarily unavailable.",
        ) from exc
    try:
        url = google_authorization_url(state)
    except Exception as exc:
        _delete_oauth_state(state)
        logger.warning("Google authorize URL failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail connect is temporarily unavailable.",
        ) from exc
    return MicrosoftStartResponse(authorization_url=url)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Finish Google login and send the browser back to the configured frontend.

    Success lands on `{FRONTEND_URL}/inbox?connected=1&email=...`.
    Failure lands on `{FRONTEND_URL}/inbox?connected=0&error=...`.
    The error is one of google_denied, invalid_state, token_exchange,
    mailbox_taken, or mailbox_conflict. Provider text, codes, and tokens are
    not copied into the URL. Unexpected failures also redirect to token_exchange
    unless the mailbox row was already committed, in which case the redirect is
    connected=1 so a saved refresh token is not described as a failed exchange.
    """
    del error_description  # logged nowhere and never copied into the redirect
    persisted = False
    try:
        if error:
            if state:
                _consume_oauth_user_id(state)
            return _inbox_failure(_ERR_GOOGLE_DENIED, provider="Google")
        if not code or not state:
            if state:
                _consume_oauth_user_id(state)
            return _inbox_failure(_ERR_STATE, provider="Google")

        user_id = _consume_oauth_user_id(state)
        if user_id is _UNAVAILABLE or user_id is None or not isinstance(user_id, UUID):
            return _inbox_failure(_ERR_STATE, provider="Google")
        if session.get(User, user_id) is None:
            return _inbox_failure(_ERR_STATE, provider="Google")

        result = exchange_authorization_code(code)
        if result is None or "access_token" not in result:
            return _inbox_failure(_ERR_TOKEN, provider="Google")
        refresh_token = result.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return _inbox_failure(_ERR_TOKEN, provider="Google")
        try:
            expires_in = int(result["expires_in"])
        except (KeyError, TypeError, ValueError):
            return _inbox_failure(_ERR_TOKEN, provider="Google")

        identity = fetch_mailbox_identity(result["access_token"])
        if identity is None:
            return _inbox_failure(_ERR_TOKEN, provider="Google")
        outcome = connect_google_mailbox(
            session,
            user_id=user_id,
            provider_account_id=identity.provider_account_id,
            email=identity.email,
            access_token=result["access_token"],
            refresh_token=refresh_token,
            expires_at=utcnow() + timedelta(seconds=expires_in),
            scopes=result.get("scope") if isinstance(result.get("scope"), str) else None,
        )
        if outcome.status is ConnectStatus.mailbox_taken:
            return _inbox_failure(_ERR_TAKEN, provider="Google")
        if outcome.status is ConnectStatus.mailbox_conflict:
            return _inbox_failure(_ERR_CONFLICT, provider="Google")
        if outcome.status is not ConnectStatus.connected or outcome.account_id is None:
            return _inbox_failure(_ERR_TOKEN, provider="Google")
        persisted = True
        try:
            await get_arq_enqueue(request, outcome.account_id, outcome.generation)
        except Exception as exc:
            logger.warning(
                "sync enqueue failed account=%s error_type=%s",
                outcome.account_id,
                type(exc).__name__,
            )
            if outcome.generation is not None:
                mark_sync_enqueue_failed(session, outcome.account_id, outcome.generation)
        return _inbox_redirect(connected="1", email=outcome.email or identity.email)
    except Exception as exc:
        logger.warning("Google callback failed error_type=%s", type(exc).__name__)
        if persisted:
            return _inbox_redirect(connected="1")
        return _inbox_failure(_ERR_TOKEN, provider="Google")


@router.post("/google/disconnect")
def google_disconnect(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> DisconnectResponse:
    """Revoke the Google grant and clear the active Gmail mailbox.

    Messages stay in the database and stay hidden. A Microsoft mailbox is left
    alone. Revoke failures are logged without the token; the local tokens are
    still deleted so this API cannot refresh access.
    """
    account = lock_active_mailbox(session, user.id, provider="google")
    if account is None:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mailbox is connected.",
        )
    token = account.refresh_token or account.access_token
    copied = token if isinstance(token, str) and token else None
    session.rollback()
    if copied:
        revoke_google_token(copied)
    if not disconnect_active_mailbox(session, user.id, provider="google"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mailbox is connected.",
        )
    return DisconnectResponse(disconnected=True)


@router.post("/microsoft/disconnect")
def microsoft_disconnect(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> DisconnectResponse:
    """Clear the active Outlook mailbox's tokens and hide it. Messages stay in the database."""
    if not disconnect_active_mailbox(session, user.id, provider="microsoft"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No mailbox is connected.",
        )
    return DisconnectResponse(disconnected=True)


@router.get("/microsoft/callback")
async def microsoft_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Finish Microsoft login and send the browser back to the configured frontend.

    Success lands on `{FRONTEND_URL}/inbox?connected=1&email=...`.
    Failure lands on `{FRONTEND_URL}/inbox?connected=0&error=...`.
    The error is one of microsoft_denied, invalid_state, token_exchange,
    mailbox_taken, or mailbox_conflict. Provider text, codes, and tokens are
    not copied into the URL. Unexpected failures also redirect to token_exchange
    unless the mailbox row was already committed, in which case the redirect is
    connected=1 so a saved refresh token is not described as a failed exchange.
    """
    del error_description  # logged nowhere and never copied into the redirect
    persisted = False
    try:
        if error:
            if state:
                _consume_oauth_user_id(state)
            return _inbox_failure(_ERR_DENIED)
        if not code or not state:
            if state:
                _consume_oauth_user_id(state)
            return _inbox_failure(_ERR_STATE)

        user_id = _consume_oauth_user_id(state)
        if user_id is _UNAVAILABLE or user_id is None:
            return _inbox_failure(_ERR_STATE)
        if not isinstance(user_id, UUID):
            return _inbox_failure(_ERR_STATE)
        if session.get(User, user_id) is None:
            return _inbox_failure(_ERR_STATE)

        result = _exchange_code(code)
        if result is None or "error" in result or "access_token" not in result:
            return _inbox_failure(_ERR_TOKEN)

        refresh_token = result.get("refresh_token")
        claims = result.get("id_token_claims") or {}
        if not isinstance(claims, dict):
            claims = {}
        provider_account_id = claims.get("oid")
        email = claims.get("preferred_username") or claims.get("email")
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(provider_account_id, str)
            or not provider_account_id
            or not isinstance(email, str)
            or not email
        ):
            return _inbox_failure(_ERR_TOKEN)
        try:
            expires_in = int(result["expires_in"])
        except (KeyError, TypeError, ValueError):
            return _inbox_failure(_ERR_TOKEN)
        tenant = claims.get("tid")
        tenant_id = tenant if isinstance(tenant, str) else None
        outcome = connect_microsoft_mailbox(
            session,
            user_id=user_id,
            provider_account_id=provider_account_id,
            tenant_id=tenant_id,
            email=email,
            access_token=result["access_token"],
            refresh_token=refresh_token,
            expires_at=utcnow() + timedelta(seconds=expires_in),
            scopes=result.get("scope") if isinstance(result.get("scope"), str) else None,
        )
        if outcome.status is ConnectStatus.mailbox_taken:
            return _inbox_failure(_ERR_TAKEN)
        if outcome.status is ConnectStatus.mailbox_conflict:
            return _inbox_failure(_ERR_CONFLICT)
        if outcome.status is not ConnectStatus.connected or outcome.account_id is None:
            return _inbox_failure(_ERR_TOKEN)
        persisted = True
        try:
            await get_arq_enqueue(request, outcome.account_id, outcome.generation)
        except Exception as exc:
            logger.warning(
                "sync enqueue failed account=%s error_type=%s",
                outcome.account_id,
                type(exc).__name__,
            )
            if outcome.generation is not None:
                mark_sync_enqueue_failed(session, outcome.account_id, outcome.generation)
        return _inbox_redirect(connected="1", email=outcome.email or email)
    except Exception as exc:
        logger.warning("Microsoft callback failed error_type=%s", type(exc).__name__)
        if persisted:
            return _inbox_redirect(connected="1")
        return _inbox_failure(_ERR_TOKEN)


async def get_arq_enqueue(request: Request, account_id: UUID, generation: int | None) -> None:
    await get_arq(request).enqueue_job("sync_mailbox_delta", str(account_id), generation)
