"""Central Redis-backed rate limiter (Phase 8).

Replaces/consolidates app/auth/rate_limit.py's Phase 3 stub (which
covered only /login and /refresh, IP-keyed, no Retry-After header). This
module is now the single limiter every route in the API uses.

Keying: "user:<id>:practice:<id>" when the caller is already
authenticated (UserRateLimiter) — the limit travels with the account,
not the network address, which matters once a practice sits behind one
shared clinic IP/NAT. Pre-auth routes (login, refresh — no verified
identity yet) fall back to "ip:<addr>" (IpRateLimiter). The WebSocket
/stream connect path (app/api/sessions.py) can't express itself as a
FastAPI Depends() at all, so it calls check_rate_limit() directly.

Fixed-window counters via Redis INCR/EXPIRE. Redis is shared by every
app-server process/instance (see REDIS_URL), so a bucket's limit holds
cluster-wide, not just within one process — a deployer scaling `app`
horizontally does not get a free multiplier on any bucket's ceiling.

On a hit: increments RATE_LIMIT_HITS_TOTAL{bucket} and logs a PHI-free
warning. Deliberately NOT written to audit_logs — AuditLog rows require
a practice_id (TenantMixin, non-nullable), which an anonymous IP-keyed
hit on /login has no way to supply, and writing to Postgres is exactly
the wrong thing to do while trying to shed load in the first place. The
metric + log line are the intended observability path for this event.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import get_current_user
from app.config import Settings, get_settings
from app.models import User
from app.ops.metrics import RATE_LIMIT_HITS_TOTAL

logger = logging.getLogger(__name__)

# bucket -> (Settings attribute for the limit, Settings attribute for the
# window). Mirrors the dict-based type->code lookup convention already
# used by app/services/transcription_service.py's _ERROR_CODE_BY_EXCEPTION.
_BUCKET_SETTINGS: dict[str, tuple[str, str]] = {
    "login": ("RATELIMIT_LOGIN_LIMIT", "RATELIMIT_LOGIN_WINDOW_SECONDS"),
    "refresh": ("RATELIMIT_REFRESH_LIMIT", "RATELIMIT_REFRESH_WINDOW_SECONDS"),
    "session_create": ("RATELIMIT_SESSION_CREATE_LIMIT", "RATELIMIT_SESSION_CREATE_WINDOW_SECONDS"),
    "note_generate": ("RATELIMIT_NOTE_GENERATE_LIMIT", "RATELIMIT_NOTE_GENERATE_WINDOW_SECONDS"),
    "ws_connect": ("RATELIMIT_WS_CONNECT_LIMIT", "RATELIMIT_WS_CONNECT_WINDOW_SECONDS"),
    "read": ("RATELIMIT_READ_LIMIT", "RATELIMIT_READ_WINDOW_SECONDS"),
}


@lru_cache
def get_redis_client() -> redis.Redis:
    return redis.from_url(get_settings().REDIS_URL)


def _limit_and_window(bucket: str, settings: Settings) -> tuple[int, int]:
    limit_attr, window_attr = _BUCKET_SETTINGS[bucket]
    return int(getattr(settings, limit_attr)), int(getattr(settings, window_attr))


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


async def check_rate_limit(
    *, bucket: str, subject: str, settings: Settings | None = None
) -> RateLimitResult:
    """The core check, usable outside FastAPI's Depends() machinery too
    (see app/api/sessions.py's WS connect-rate-limit call). `subject`
    is caller-supplied so HTTP dependencies and the WS route can each
    build their own key (user-scoped vs IP-scoped) without this function
    needing to know about Request/WebSocket at all.
    """
    settings = settings or get_settings()
    if not settings.RATELIMIT_ENABLED:
        return RateLimitResult(allowed=True, retry_after_seconds=0)

    limit, window_seconds = _limit_and_window(bucket, settings)
    client = get_redis_client()
    key = f"ratelimit:{bucket}:{subject}"

    count = await client.incr(key)
    if count == 1:
        await client.expire(key, window_seconds)
        retry_after = window_seconds
    else:
        ttl = await client.ttl(key)
        retry_after = ttl if ttl > 0 else window_seconds

    if count > limit:
        RATE_LIMIT_HITS_TOTAL.labels(bucket=bucket).inc()
        logger.warning("rate limit exceeded", extra={"bucket": bucket, "limit": limit})
        return RateLimitResult(allowed=False, retry_after_seconds=max(retry_after, 1))

    return RateLimitResult(allowed=True, retry_after_seconds=0)


def _too_many_requests(result: RateLimitResult) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests, please try again later.",
        headers={"Retry-After": str(result.retry_after_seconds)},
    )


class IpRateLimiter:
    """FastAPI dependency for pre-auth routes (login, refresh) — keys by
    client IP since there is no verified account yet to key on.
    """

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        result = await check_rate_limit(bucket=self.bucket, subject=f"ip:{client_ip}")
        if not result.allowed:
            raise _too_many_requests(result)


class UserRateLimiter:
    """FastAPI dependency for authenticated routes — keys by
    user+practice so the limit travels with the account rather than the
    network address. Depends on get_current_user itself, which FastAPI
    de-duplicates against the route's own get_current_user dependency
    within one request (no double auth cost).
    """

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    async def __call__(self, user: Annotated[User, Depends(get_current_user)]) -> None:
        result = await check_rate_limit(
            bucket=self.bucket, subject=f"user:{user.id}:practice:{user.practice_id}"
        )
        if not result.allowed:
            raise _too_many_requests(result)


__all__ = [
    "IpRateLimiter",
    "RateLimitResult",
    "UserRateLimiter",
    "check_rate_limit",
    "get_redis_client",
]
