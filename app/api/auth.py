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


class MicrosoftCallbackResponse(BaseModel):
    connected: bool = True
    email: str


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


@router.get("/microsoft/start")
def microsoft_start(user: User = Depends(get_current_user)) -> RedirectResponse:
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
    return RedirectResponse(url, status_code=302)


@router.get("/microsoft/callback")
async def microsoft_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> MicrosoftCallbackResponse:
    settings = get_settings()
    raw = None

    if state:
        client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        )
        try:
            raw = client.getdel(f"oauth_state:{state}")
        finally:
            client.close()
    
    if error or not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
         detail="Microsoft authentication failed"
         )

    try: 
        user_id = UUID(raw.decode()) if raw else None 
    except (UnicodeDecodeError, ValueError):
        user_id = None
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
        detail="Microsoft sign-in was not completed",
        )
    
    result = build_msal_app().acquire_token_by_authorization_code(
        code,
        scopes=settings.graph_scope_list,
        redirect_uri=settings.graph_redirect_uri,
    )
    if "error" in result or "access_token" not in result:
        logger.warning("Microsoft token exchange failed: %s", result.get("error"))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
        detail="Microsoft sign-in was not completed",
        )

    refresh_token = result.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
        detail="Microsoft sign-in was not completed"
        )
    claims = result.get("id_token_claims") or {}
    provider_account_id = claims.get("oid")
    email = claims.get("preferred_username") or claims.get("email")
    if not provider_account_id or not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
        detail="Microsoft sign-in was not completed",
        )
    expires_at = utcnow() + timedelta(seconds=int(result["expires_in"]))
    account = session.exec(
        select(ConnectedAccount).where(
            ConnectedAccount.provider == "microsoft",
            ConnectedAccount.provider_account_id == provider_account_id,
        )
    ) .first()

    if account is not None and account.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
        detail="This mailbox is already connected to a different account",
        )
    if account is None:
        account = ConnectedAccount(
            user_id = user_id,
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
    
    return MicrosoftCallbackResponse(email=email)