"""JWT encode/decode. Stateless by design: an access token carries every
claim a request needs (identity, tenant, role) so no server-side session
lookup is required on the hot path — see the README ("Why stateless").

Refresh tokens are also JWTs (so their authenticity is self-verifying),
but are additionally tracked in the refresh_tokens table so they can be
revoked/rotated — the JWT alone can prove *who signed it*, not *whether
it's still valid*.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import get_settings
from app.models.enums import UserRole

ACCESS = "access"
REFRESH = "refresh"
MFA_PENDING = "mfa_pending"

# Not in Settings: this is an internal implementation detail of the login
# flow, not something a deployer should need to tune.
MFA_PENDING_TTL_MINUTES = 5


class TokenError(Exception):
    """Raised for any invalid, expired, or wrong-type token."""


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    practice_id: uuid.UUID
    role: UserRole
    jti: str


@dataclass(frozen=True)
class RefreshTokenClaims:
    user_id: uuid.UUID
    practice_id: uuid.UUID
    jti: str


@dataclass(frozen=True)
class MfaPendingClaims:
    user_id: uuid.UUID
    practice_id: uuid.UUID
    jti: str


def _encode(payload: dict[str, Any], ttl: timedelta) -> tuple[str, str, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    exp = now + ttl
    jti = str(uuid.uuid4())
    full_payload = {
        **payload,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(full_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, jti, exp


def create_access_token(
    user_id: uuid.UUID,
    practice_id: uuid.UUID,
    role: UserRole,
    *,
    ttl_minutes: int | None = None,
) -> str:
    settings = get_settings()
    ttl = timedelta(
        minutes=ttl_minutes if ttl_minutes is not None else settings.ACCESS_TOKEN_TTL_MINUTES
    )
    token, _, _ = _encode(
        {
            "sub": str(user_id),
            "practice_id": str(practice_id),
            "role": role.value,
            "type": ACCESS,
        },
        ttl,
    )
    return token


def create_refresh_token(
    user_id: uuid.UUID,
    practice_id: uuid.UUID,
    *,
    ttl_days: int | None = None,
) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at); caller persists jti/expires_at."""
    settings = get_settings()
    ttl = timedelta(days=ttl_days if ttl_days is not None else settings.REFRESH_TOKEN_TTL_DAYS)
    return _encode({"sub": str(user_id), "practice_id": str(practice_id), "type": REFRESH}, ttl)


def create_mfa_pending_token(user_id: uuid.UUID, practice_id: uuid.UUID) -> str:
    token, _, _ = _encode(
        {"sub": str(user_id), "practice_id": str(practice_id), "type": MFA_PENDING},
        timedelta(minutes=MFA_PENDING_TTL_MINUTES),
    )
    return token


def _decode_raw(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    return payload


def decode_access_token(token: str) -> AccessTokenClaims:
    payload = _decode_raw(token)
    if payload.get("type") != ACCESS:
        raise TokenError("expected an access token")
    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            practice_id=uuid.UUID(payload["practice_id"]),
            role=UserRole(payload["role"]),
            jti=payload["jti"],
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed access token claims") from exc


def decode_refresh_token(token: str) -> RefreshTokenClaims:
    payload = _decode_raw(token)
    if payload.get("type") != REFRESH:
        raise TokenError("expected a refresh token")
    try:
        return RefreshTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            practice_id=uuid.UUID(payload["practice_id"]),
            jti=payload["jti"],
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed refresh token claims") from exc


def decode_mfa_pending_token(token: str) -> MfaPendingClaims:
    payload = _decode_raw(token)
    if payload.get("type") != MFA_PENDING:
        raise TokenError("expected an mfa_pending token")
    try:
        return MfaPendingClaims(
            user_id=uuid.UUID(payload["sub"]),
            practice_id=uuid.UUID(payload["practice_id"]),
            jti=payload["jti"],
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("malformed mfa_pending token claims") from exc


def peek_token_type(token: str) -> str:
    """The token's `type` claim, without enforcing which one it must be.

    POST /v1/auth/mfa/verify legitimately accepts either an access token
    (confirming initial MFA setup) or an mfa_pending token (completing a
    login) and dispatches on which one it got.
    """
    payload = _decode_raw(token)
    token_type = payload.get("type")
    if not isinstance(token_type, str):
        raise TokenError("missing token type claim")
    return token_type
