"""Migrations apply and roll back cleanly."""

import asyncio

from sqlalchemy import inspect
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


def test_upgrade_then_downgrade_round_trip(admin_url: str) -> None:
    """head -> base -> head, leaving the schema at head either way.

    A plain sync test function deliberately: alembic's command.upgrade/
    downgrade each run their own asyncio.run() internally, which would
    raise if this ran as an `async def` test under pytest-asyncio's
    already-active event loop.
    """
    assert asyncio.run(_table_names(admin_url)) >= EXPECTED_TABLES

    try:
        command.downgrade(_alembic_config(admin_url), "base")
        after_downgrade = asyncio.run(_table_names(admin_url))
        assert "practices" not in after_downgrade
        assert "audit_logs" not in after_downgrade
    finally:
        command.upgrade(_alembic_config(admin_url), "head")

    assert asyncio.run(_table_names(admin_url)) >= EXPECTED_TABLES
