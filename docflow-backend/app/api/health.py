"""Liveness and readiness endpoints."""

import logging
from typing import Annotated, Any

import asyncpg
import redis.asyncio as redis
from fastapi import APIRouter, Depends, Response

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


def _asyncpg_dsn(database_url: str) -> str:
    """asyncpg does not understand the '+asyncpg' driver suffix some DSNs use."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _check_postgres(database_url: str) -> bool:
    conn = None
    try:
        conn = await asyncpg.connect(dsn=_asyncpg_dsn(database_url), timeout=5)
        await conn.execute("SELECT 1")
        return True
    except Exception:
        logger.exception("Postgres readiness check failed")
        return False
    finally:
        if conn is not None:
            await conn.close()


async def _check_redis(redis_url: str) -> bool:
    client = None
    try:
        client = redis.from_url(redis_url, socket_connect_timeout=5)
        pong = await client.ping()
        return bool(pong)
    except Exception:
        logger.exception("Redis readiness check failed")
        return False
    finally:
        if client is not None:
            await client.aclose()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — always returns ok if the process is up."""
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Readiness probe — verifies dependency connectivity."""
    postgres_ok = await _check_postgres(settings.DATABASE_URL)
    redis_ok = await _check_redis(settings.REDIS_URL)

    all_ok = postgres_ok and redis_ok
    response.status_code = 200 if all_ok else 503

    return {
        "status": "ok" if all_ok else "unavailable",
        "dependencies": {
            "postgres": "ok" if postgres_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    }
