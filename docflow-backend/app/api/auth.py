"""Auth endpoints, mounted at /v1/auth.

Access tokens are returned in the JSON body, never as an httpOnly cookie.
The Chrome extension and the mobile app have no reliable access to
browser cookie storage (and the extension's background service worker
talks to the API from a chrome-extension:// origin, not the web app's
origin), so cookie-based sessions can't work identically across all three
clients. Bearer-in-body-then-Authorization-header works the same way for
all of them, which is the whole point of a stateless design — see the
README ("Why stateless").
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.audit import record_auth_event
from app.auth.dependencies import (
    get_bearer_token,
    get_current_user,
    get_tenant_session,
    unauthorized_error,
)
from app.auth.mfa import generate_totp_secret, provisioning_uri, verify_totp_code
from app.auth.passwords import hash_password, verify_password
from app.auth.refresh_store import (
    RefreshTokenError,
    RefreshTokenReuseDetected,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.auth.tokens import (
    ACCESS,
    MFA_PENDING,
    TokenError,
    create_access_token,
    create_mfa_pending_token,
    decode_access_token,
    decode_mfa_pending_token,
    decode_refresh_token,
    peek_token_type,
)
from app.config import get_settings
from app.db.session import get_admin_sessionmaker, get_session, get_sessionmaker, set_tenant
from app.models import Practice, User
from app.models.enums import AuditAction, UserRole
from app.ops.ratelimit import IpRateLimiter

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_GENERIC_LOGIN_ERROR = "Invalid email or password"


# --- Schemas -----------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=1, max_length=255)
    practice_name: str = Field(min_length=1, max_length=255)


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    mfa_required: bool
    token_type: str = "bearer"
    access_token: str | None = None
    refresh_token: str | None = None
    mfa_pending_token: str | None = None


class MfaSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class MfaVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class MfaVerifyResponse(BaseModel):
    mfa_enabled: bool
    token_type: str = "bearer"
    access_token: str | None = None
    refresh_token: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LogoutRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    status: str = "ok"


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    practice_id: uuid.UUID
    mfa_enabled: bool
    is_active: bool


# --- Helpers -------------------------------------------------------------


async def _find_user_by_email(email: str) -> User | None:
    """Cross-tenant lookup by email — the one legitimate use of the admin
    engine in the request path. See app/db/session.get_admin_engine.
    """
    async with get_admin_sessionmaker()() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


# --- Routes ----------------------------------------------------------------


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenPairResponse:
    """Bootstraps a new practice together with its first (owner) user."""
    practice_id = uuid.uuid4()

    try:
        await session.begin()
        practice = Practice(id=practice_id, name=body.practice_name)
        session.add(practice)
        await session.flush()

        # The practice now exists; scope the rest of this transaction to
        # it so the WITH CHECK clause on users' RLS policy is satisfiable.
        await set_tenant(session, practice_id)

        user = User(
            practice_id=practice_id,
            email=body.email,
            hashed_password=hash_password(body.password),
            full_name=body.full_name,
            role=UserRole.owner,
        )
        session.add(user)
        await session.flush()

        refresh_token = await issue_refresh_token(session, user_id=user.id, practice_id=practice_id)
        await record_auth_event(
            session,
            practice_id=practice_id,
            action=AuditAction.register,
            actor_user_id=user.id,
            request=request,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from None

    access_token = create_access_token(user.id, practice_id, user.role)
    return TokenPairResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/login", dependencies=[Depends(IpRateLimiter("login"))])
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    user = await _find_user_by_email(body.email)

    if (
        user is None
        or user.hashed_password is None
        or not verify_password(body.password, user.hashed_password)
    ):
        # Generic message either way — never reveal whether the email is
        # registered. We only have a tenant to audit-log against when the
        # user *was* found; an unknown email leaves nothing to attribute
        # the attempt to (the rate limiter is what guards that case).
        if user is not None:
            async with get_sessionmaker()() as session:
                await session.begin()
                await set_tenant(session, user.practice_id)
                await record_auth_event(
                    session,
                    practice_id=user.practice_id,
                    action=AuditAction.login_failure,
                    actor_user_id=user.id,
                    request=request,
                )
                await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _GENERIC_LOGIN_ERROR)

    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _GENERIC_LOGIN_ERROR)

    async with get_sessionmaker()() as session:
        await session.begin()
        await set_tenant(session, user.practice_id)

        if user.mfa_enabled:
            mfa_pending_token = create_mfa_pending_token(user.id, user.practice_id)
            await record_auth_event(
                session,
                practice_id=user.practice_id,
                action=AuditAction.login_success,
                actor_user_id=user.id,
                request=request,
                metadata={"mfa_pending": True},
            )
            await session.commit()
            return LoginResponse(mfa_required=True, mfa_pending_token=mfa_pending_token)

        access_token = create_access_token(user.id, user.practice_id, user.role)
        refresh_token = await issue_refresh_token(
            session, user_id=user.id, practice_id=user.practice_id
        )
        await record_auth_event(
            session,
            practice_id=user.practice_id,
            action=AuditAction.login_success,
            actor_user_id=user.id,
            request=request,
        )
        await session.commit()

    return LoginResponse(mfa_required=False, access_token=access_token, refresh_token=refresh_token)


@router.post("/mfa/setup")
async def mfa_setup(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> MfaSetupResponse:
    """Generates (but does not yet enable) a TOTP secret. Enabling happens
    only once POST /mfa/verify confirms the user can produce a valid code —
    otherwise a typo here could lock the user out of their own account.
    """
    secret = generate_totp_secret()
    user.mfa_secret = secret
    await session.flush()
    return MfaSetupResponse(secret=secret, provisioning_uri=provisioning_uri(secret, user.email))


@router.post("/mfa/verify")
async def mfa_verify(
    body: MfaVerifyRequest,
    request: Request,
    token: Annotated[str, Depends(get_bearer_token)],
) -> MfaVerifyResponse:
    """Dual-purpose, dispatched on the presented token's type:

    - An *access* token means "confirm the MFA setup I just started" —
      response carries no new tokens, the caller already has valid ones.
    - An *mfa_pending* token means "complete a login that required MFA" —
      response carries fresh access+refresh tokens, exactly like a normal
      password-only login would have.
    """
    try:
        token_type = peek_token_type(token)
    except TokenError:
        raise unauthorized_error("Invalid or expired token") from None

    if token_type == ACCESS:
        claims = decode_access_token(token)
        async with get_sessionmaker()() as session:
            await session.begin()
            await set_tenant(session, claims.practice_id)
            user = await session.get(User, claims.user_id)
            if user is None or not user.is_active or user.mfa_secret is None:
                await session.rollback()
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA setup has not been started")
            if not verify_totp_code(user.mfa_secret, body.code):
                await session.rollback()
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid MFA code")

            user.mfa_enabled = True
            await record_auth_event(
                session,
                practice_id=user.practice_id,
                action=AuditAction.mfa_enrolled,
                actor_user_id=user.id,
                request=request,
            )
            await session.commit()
        return MfaVerifyResponse(mfa_enabled=True)

    if token_type == MFA_PENDING:
        pending_claims = decode_mfa_pending_token(token)
        async with get_sessionmaker()() as session:
            await session.begin()
            await set_tenant(session, pending_claims.practice_id)
            user = await session.get(User, pending_claims.user_id)
            if (
                user is None
                or not user.is_active
                or not user.mfa_enabled
                or user.mfa_secret is None
            ):
                await session.rollback()
                raise unauthorized_error("Invalid or expired token")

            if not verify_totp_code(user.mfa_secret, body.code):
                await record_auth_event(
                    session,
                    practice_id=user.practice_id,
                    action=AuditAction.login_failure,
                    actor_user_id=user.id,
                    request=request,
                    metadata={"stage": "mfa"},
                )
                await session.commit()
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid MFA code")

            access_token = create_access_token(user.id, user.practice_id, user.role)
            refresh_token = await issue_refresh_token(
                session, user_id=user.id, practice_id=user.practice_id
            )
            await record_auth_event(
                session,
                practice_id=user.practice_id,
                action=AuditAction.mfa_verified,
                actor_user_id=user.id,
                request=request,
            )
            await session.commit()
        return MfaVerifyResponse(
            mfa_enabled=True, access_token=access_token, refresh_token=refresh_token
        )

    raise unauthorized_error("Invalid or expired token")


@router.post("/refresh", dependencies=[Depends(IpRateLimiter("refresh"))])
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RefreshResponse:
    settings = get_settings()
    try:
        claims = decode_refresh_token(body.refresh_token)
    except TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None

    await session.begin()
    await set_tenant(session, claims.practice_id)

    try:
        new_refresh_token = await rotate_refresh_token(
            session, claims, idle_timeout_minutes=settings.IDLE_TIMEOUT_MINUTES
        )
    except RefreshTokenReuseDetected as exc:
        # The chain is already revoked inside rotate_refresh_token; commit
        # so that revocation (and this audit entry) survive the 401 we're
        # about to raise, rather than being rolled back with it.
        await record_auth_event(
            session,
            practice_id=claims.practice_id,
            action=AuditAction.token_reuse_detected,
            actor_user_id=exc.user_id,
            request=request,
        )
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None
    except RefreshTokenError:
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from None

    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        await session.rollback()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    new_access_token = create_access_token(user.id, user.practice_id, user.role)
    await session.commit()

    return RefreshResponse(access_token=new_access_token, refresh_token=new_refresh_token)


@router.post("/logout")
async def logout(
    body: LogoutRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> LogoutResponse:
    try:
        claims = decode_refresh_token(body.refresh_token)
    except TokenError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid refresh token") from None
    if claims.user_id != user.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Refresh token does not belong to the current user"
        )

    await revoke_refresh_token(session, claims.jti)
    await record_auth_event(
        session,
        practice_id=user.practice_id,
        action=AuditAction.logout,
        actor_user_id=user.id,
        request=request,
    )
    return LogoutResponse()


@router.get("/me")
async def me(user: Annotated[User, Depends(get_current_user)]) -> MeResponse:
    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        practice_id=user.practice_id,
        mfa_enabled=user.mfa_enabled,
        is_active=user.is_active,
    )
