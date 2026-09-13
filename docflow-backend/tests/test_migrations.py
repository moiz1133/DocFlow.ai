"""Migrations apply and roll back cleanly."""

import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.conftest import _alembic_config

EXPECTED_TABLES = {
    "practices",
    "users",
    "sessions",
    "transcripts",
    "notes",
    "consents",
    "audit_logs",
    "alembic_version",
}


def _get_table_names(sync_conn: Connection) -> set[str]:
    return set(inspect(sync_conn).get_table_names())


async def _table_names(admin_url: str) -> set[str]:
    engine = create_async_engine(admin_url)
    async with engine.connect() as conn:
        names: set[str] = await conn.run_sync(_get_table_names)
    await engine.dispose()
    return names


async def _clear_audit_logs(admin_url: str) -> None:
    """Other tests in this shared session-scoped database write real
    audit_logs rows using the Phase 3 auth enum values (register,
    login_success, ...). Downgrading past that migration shrinks
    audit_action back down and would legitimately fail if any row still
    referenced a value being removed — clear the table first so this
    test's downgrade always exercises the empty-database happy path,
    regardless of what other tests ran before it.
    """
    engine = create_async_engine(admin_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM audit_logs"))
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine_around_round_trip() -> AsyncIterator[None]:
    """The round-trip test below drops and recreates every table via raw
    alembic commands on their own ad-hoc asyncio.run() loops (see that
    test's docstring). app.db.session.get_engine() AND get_admin_engine()
    are both process-wide @lru_cache pools other tests reuse (the `client`
    fixture's requests use get_engine() via get_sessionmaker(); the login
    route's cross-tenant email lookup — app/api/auth.py's
    _find_user_by_email — specifically uses get_admin_sessionmaker(), i.e.
    get_admin_engine()); any connection either pool already opened before
    the drop keeps asyncpg's client-side prepared-statement cache pointing
    at the now-gone table/type OIDs, so the next query through that pool
    raises InvalidCachedStatementError — observed via a rate-limit test
    that repeatedly calls POST /v1/auth/login (Phase 8). Disposing both
    here — on this fixture's own (correct, session-scoped) event loop,
    unlike the test's ad-hoc loops — forces fresh connections afterward.
    """
    yield
    from app.db.session import get_admin_engine, get_engine

    await get_engine().dispose()
    await get_admin_engine().dispose()


def test_upgrade_then_downgrade_round_trip(admin_url: str) -> None:
    """head -> base -> head, leaving the schema at head either way.

    A plain sync test function deliberately: alembic's command.upgrade/
    downgrade each run their own asyncio.run() internally, which would
    raise if this ran as an `async def` test under pytest-asyncio's
    already-active event loop.
    """
    assert asyncio.run(_table_names(admin_url)) >= EXPECTED_TABLES
    asyncio.run(_clear_audit_logs(admin_url))

    try:
        command.downgrade(_alembic_config(admin_url), "base")
        after_downgrade = asyncio.run(_table_names(admin_url))
        assert "practices" not in after_downgrade
        assert "audit_logs" not in after_downgrade
    finally:
        command.upgrade(_alembic_config(admin_url), "head")

    assert asyncio.run(_table_names(admin_url)) >= EXPECTED_TABLES
