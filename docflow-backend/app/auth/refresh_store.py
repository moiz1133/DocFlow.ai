"""Refresh-token persistence: issuance, rotation, and reuse detection.

Rotation model: each successful /refresh call revokes the presented jti
and issues a brand new one (`revoked` acts as "already used"). If a jti
that's already marked revoked is presented again, that can only mean one
of two things — a client retried after a partial rotation (rare, and this
design doesn't try to be clever about it), or a stolen refresh token is
being used after the legitimate client already rotated past it. Either
way, the safe response is the same: revoke every refresh token this user
holds and force a fresh login everywhere.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import RefreshTokenClaims, create_refresh_token
from app.models import RefreshToken


class RefreshTokenError(Exception):
    """Refresh refused (unknown/expired/idle) — plain 401, no side effects
    beyond revoking the single token involved, where applicable."""


class RefreshTokenReuseDetected(Exception):
    """A previously-rotated jti was presented again. Carries the affected
    user_id so the caller can audit-log and the whole chain has already
    been revoked by the time this is raised."""

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__("refresh token reuse detected")
        self.user_id = user_id


async def issue_refresh_token(
    session: AsyncSession, *, user_id: uuid.UUID, practice_id: uuid.UUID
) -> str:
    token, jti, expires_at = create_refresh_token(user_id, practice_id)
    session.add(
        RefreshToken(
            practice_id=practice_id,
            user_id=user_id,
            jti=jti,
            expires_at=expires_at,
        )
    )
    await session.flush()
    return token


async def rotate_refresh_token(
    session: AsyncSession,
    claims: RefreshTokenClaims,
    *,
    idle_timeout_minutes: int,
) -> str:
    """Validates the presented refresh token and, on success, returns a
    freshly issued replacement token string (the old jti is revoked).

    Raises RefreshTokenError or RefreshTokenReuseDetected on failure —
    both are the caller's cue to return 401; only the latter also implies
    every other refresh token for this user is now revoked.
    """
    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == claims.jti))
    row = result.scalar_one_or_none()

    if row is None:
        raise RefreshTokenError("unknown refresh token")

    if row.revoked:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == row.user_id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        await session.flush()
        raise RefreshTokenReuseDetected(row.user_id)

    now = datetime.now(UTC)

    if row.expires_at < now:
        raise RefreshTokenError("refresh token expired")

    if row.last_activity_at < now - timedelta(minutes=idle_timeout_minutes):
        row.revoked = True
        await session.flush()
        raise RefreshTokenError("idle timeout exceeded")

    row.revoked = True
    new_token = await issue_refresh_token(session, user_id=row.user_id, practice_id=row.practice_id)
    return new_token


async def revoke_refresh_token(session: AsyncSession, jti: str) -> None:
    """Used by logout: revoke a single token regardless of its current state."""
    await session.execute(update(RefreshToken).where(RefreshToken.jti == jti).values(revoked=True))
    await session.flush()
