"""Async engine/session wiring for the application's runtime DB role.

Deliberately uses Settings.APP_DATABASE_URL (the restricted, non-superuser,
NOBYPASSRLS role) rather than Settings.DATABASE_URL (the migration/admin
role) — see app/config.py and the README for why the distinction matters
for Row-Level Security to actually be enforced.
"""

import uuid
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().APP_DATABASE_URL, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-dependency-shaped session provider for later phases."""
    async with get_sessionmaker()() as session:
        yield session


async def set_tenant(session: AsyncSession, practice_id: uuid.UUID | str) -> None:
    """Scope the current transaction to a single practice for RLS.

    Issues `SET LOCAL app.current_practice_id`, which every tenant-scoped
    table's RLS policy reads via `current_setting('app.current_practice_id',
    true)`. `SET LOCAL` only takes effect for the current transaction, so
    this must be called after the session has begun a transaction and
    before any tenant-scoped query runs in it.

    Per-request wiring — deriving practice_id from the authenticated user
    and calling this at the start of each request — lands in Phase 3.
    """
    pid = practice_id if isinstance(practice_id, uuid.UUID) else uuid.UUID(str(practice_id))
    # SET does not accept bound parameters; interpolating is safe here only
    # because pid has already been round-tripped through uuid.UUID, which
    # guarantees a well-formed hex-and-hyphen literal with no injection risk.
    await session.execute(text(f"SET LOCAL app.current_practice_id = '{pid}'"))
