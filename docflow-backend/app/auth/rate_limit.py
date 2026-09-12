"""Redis-backed fixed-window rate limiting.

Wired onto /login and /refresh per the security requirements even though
today's thresholds are lenient — the point is that the hook exists and is
load-bearing, so tightening it later is a config change, not new plumbing.
"""

from functools import lru_cache

import redis.asyncio as redis
from fastapi import HTTPException, Request, status

from app.config import get_settings


@lru_cache
def get_redis_client() -> redis.Redis:
    return redis.from_url(get_settings().REDIS_URL)


class RateLimiter:
    """FastAPI dependency: limits requests per client IP per time window."""

    def __init__(self, action: str, *, limit: int = 30, window_seconds: int = 60) -> None:
        self.action = action
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{self.action}:{client_ip}"
        client = get_redis_client()

        count = await client.incr(key)
        if count == 1:
            await client.expire(key, self.window_seconds)

        if count > self.limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests, please try again later.",
            )
