"""Registration and login. Both return a Bearer token.

These are plain `def` routes because password hashing is deliberately slow.
FastAPI runs them in a thread pool, so hashing does not block the event loop.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import AfterValidator, BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
import secrets
import redis 
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

import logging
from datetime import timedelta
from uuid import UUID


from app.api.deps import get_current_user, get_session
from app.config import get_settings
from app.models.user import User
from app.security import create_access_token, hash_password, verify_password
from app.services.graph.oauth import build_msal_app
from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow
from app.services.graph.tokens import save_tokens
from app.api.deps import get_arq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Stored and looked up in lowercase, so Ana@x.com and ana@x.com are one account.
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]


class RegisterRequest(BaseModel):
    email: NormalizedEmail
    # The upper limit stops very long passwords from tying up the hasher.
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=256)


class LoginRequest(BaseModel):
    email: NormalizedEmail
    password: str = Field(max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MicrosoftStartResponse(BaseModel):
    authorization_url: str


# The frontend branches on these. Provider text is logged and stays out of the URL.
_ERR_DENIED = "microsoft_denied"
_ERR_STATE = "invalid_state"
_ERR_TOKEN = "token_exchange"
_ERR_TAKEN = "mailbox_taken"


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=get_settings().access_token_expire_minutes * 60,
    )


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
    return _token_response(user)


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
    return _token_response(user)


def _inbox_redirect(**query: str) -> RedirectResponse:
    """302 to the Vite origin. Query values are encoded once."""
    base = get_settings().frontend_url.rstrip("/")
    url = f"{base}/inbox?{urlencode(query)}"
    response = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    # RedirectResponse quotes the location. Set the already-encoded URL so
    # the email is not encoded a second time.
    response.headers["location"] = url
    return response


def _inbox_failure(code: str, **detail: object) -> RedirectResponse:
    logger.warning("Microsoft sign-in failed (%s): %s", code, detail)
    return _inbox_redirect(connected="0", error=code)


def _consume_oauth_user_id(state: str) -> UUID | None:
    """Read and delete the one-time state, or return None when it is not ours."""
    settings = get_settings()
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        raw = client.getdel(f"oauth_state:{state}")
    finally:
        client.close()
    if not raw:
        return None
    try:
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        return UUID(text)
    except (UnicodeDecodeError, ValueError):
        return None


@router.get("/microsoft/start", status_code=status.HTTP_200_OK)
def microsoft_start(user: User = Depends(get_current_user)) -> MicrosoftStartResponse:
    """Return Microsoft's authorize URL. The Bearer token is not copied into it."""
    state = secrets.token_urlsafe(32)
    settings = get_settings()
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try: 
        client.setex(f"oauth_state:{state}", 600, str(user.id))
    finally:
        client.close()
    
    url = build_msal_app().get_authorization_request_url(
        settings.graph_scope_list,
        state=state,
        redirect_uri=settings.graph_redirect_uri,
    )
    return MicrosoftStartResponse(authorization_url=url)


@router.get("/microsoft/callback")
async def microsoft_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Finish Microsoft login and send the browser back to the Vite app.

    Success lands on `{FRONTEND_URL}/inbox?connected=1&email=...`.
    Failure lands on `{FRONTEND_URL}/inbox?connected=0&error=...` with one of
    microsoft_denied, invalid_state, token_exchange, or mailbox_taken.
    """
    if error:
        # Burn the one-time state when Microsoft sent one back with the error.
        if state:
            _consume_oauth_user_id(state)
        return _inbox_failure(
            _ERR_DENIED,
            error=error,
            error_description=error_description,
        )
    if not code or not state:
        if state:
            _consume_oauth_user_id(state)
        return _inbox_failure(_ERR_STATE, reason="missing_code_or_state")

    user_id = _consume_oauth_user_id(state)
    if user_id is None:
        return _inbox_failure(_ERR_STATE, reason="unknown_or_malformed_state")

    settings = get_settings()
    result = build_msal_app().acquire_token_by_authorization_code(
        code,
        scopes=settings.graph_scope_list,
        redirect_uri=settings.graph_redirect_uri,
    )
    if "error" in result or "access_token" not in result:
        return _inbox_failure(
            _ERR_TOKEN,
            error=result.get("error"),
            error_description=result.get("error_description"),
        )

    refresh_token = result.get("refresh_token")
    claims = result.get("id_token_claims") or {}
    provider_account_id = claims.get("oid")
    email = claims.get("preferred_username") or claims.get("email")
    if not refresh_token or not provider_account_id or not email:
        return _inbox_failure(
            _ERR_TOKEN,
            error="incomplete_token_response",
            has_refresh_token=bool(refresh_token),
            has_oid=bool(provider_account_id),
            has_email=bool(email),
        )
    try:
        expires_in = int(result["expires_in"])
    except (KeyError, TypeError, ValueError):
        return _inbox_failure(_ERR_TOKEN, error="missing_expires_in")

    expires_at = utcnow() + timedelta(seconds=expires_in)
    account = session.exec(
        select(ConnectedAccount).where(
            ConnectedAccount.provider == "microsoft",
            ConnectedAccount.provider_account_id == provider_account_id,
        )
    ).first()

    if account is not None and account.user_id != user_id:
        return _inbox_failure(_ERR_TAKEN, provider_account_id=provider_account_id)
    if account is None:
        account = ConnectedAccount(
            user_id=user_id,
            provider_account_id=provider_account_id,
            tenant_id=claims.get("tid"),
            email=email,
            access_token=result["access_token"],
        )
        session.add(account)
    else:
        account.email = email
        account.tenant_id = claims.get("tid")

    save_tokens(
        session,
        account,
        access_token=result["access_token"],
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=result.get("scope"),
    )
    await get_arq(request).enqueue_job("sync_mailbox_delta", str(account.id))
    return _inbox_redirect(connected="1", email=email)
