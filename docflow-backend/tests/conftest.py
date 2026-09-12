"""Shared pytest fixtures.

Sets required env vars before app import (hermetic defaults, independent
of whatever .env happens to contain), then provisions a throwaway Postgres
database (`docflow_test`), migrates it to head once per test session, and
exposes admin-role/app-role DSNs and session factories for tests that need
real database access (migrations, tenant isolation, audit immutability).

Requires a reachable Postgres server matching DATABASE_URL's host/port —
`make test` starts one via `docker compose up -d db` before running pytest.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://docflow:docflow@localhost:5433/docflow")
os.environ.setdefault(
    "APP_DATABASE_URL",
    "postgresql+asyncpg://docflow_app:docflow_app_dev_only@localhost:5433/docflow",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("ENV", "dev")

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from app.config import get_settings
from app.models import Practice, User
from app.models.enums import PracticeStatus, UserRole

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_NAME = "docflow_test"


def _with_dbname(url: str, dbname: str) -> str:
    """Swap the database name in a DSN, keeping scheme/host/credentials."""
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


def _alembic_config(sqlalchemy_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url)
    return config


async def _recreate_database(maintenance_url: str, name: str) -> None:
    engine = create_async_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {name}"))
    await engine.dispose()


async def _drop_database(maintenance_url: str, name: str) -> None:
    engine = create_async_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    await engine.dispose()


@pytest.fixture(scope="session")
def admin_url() -> str:
    """Admin/migration-role DSN pointed at the throwaway test database."""
    return _with_dbname(get_settings().DATABASE_URL, TEST_DB_NAME)


@pytest.fixture(scope="session")
def app_url() -> str:
    """Restricted app-role DSN pointed at the throwaway test database."""
    return _with_dbname(get_settings().APP_DATABASE_URL, TEST_DB_NAME)


@pytest.fixture(scope="session", autouse=True)
def _provisioned_database(admin_url: str) -> Iterator[None]:
    """Create docflow_test fresh and migrate it to head, once per session.

    Deliberately a *sync* fixture: alembic's command.upgrade/downgrade run
    their own asyncio.run() internally (see alembic/env.py), which raises
    if called from inside an already-running event loop — a plain sync
    fixture guarantees no loop is active yet when it runs.
    """
    maintenance_url = _with_dbname(get_settings().DATABASE_URL, "postgres")
    asyncio.run(_recreate_database(maintenance_url, TEST_DB_NAME))

    command.upgrade(_alembic_config(admin_url), "head")

    yield

    asyncio.run(_drop_database(maintenance_url, TEST_DB_NAME))


@pytest_asyncio.fixture
async def admin_sessionmaker(admin_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on the admin/migration role — superuser, bypasses RLS.

    Used only to set up cross-tenant fixture data in tests; production
    code must never use this role for queries (see README).
    """
    engine = create_async_engine(admin_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def app_sessionmaker(app_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on the restricted app role — this is what RLS actually gates."""
    engine = create_async_engine(app_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@dataclass(frozen=True)
class TwoPractices:
    practice_a_id: uuid.UUID
    practice_b_id: uuid.UUID
    clinician_a_id: uuid.UUID
    clinician_b_id: uuid.UUID


@pytest_asyncio.fixture
async def two_practices(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> TwoPractices:
    """Two practices, each with one clinician user, via the admin role.

    Uses the admin (superuser) role deliberately: seeding cross-tenant
    fixture data is exactly the kind of operation RLS's WITH CHECK would
    otherwise block under the restricted app role, since no single
    `app.current_practice_id` value satisfies rows for both tenants.
    """
    # Unique suffix: email is globally unique and this fixture is
    # function-scoped, so repeated invocations across tests must not
    # collide on the same test database.
    suffix = uuid.uuid4().hex[:8]

    async with admin_sessionmaker() as session, session.begin():
        practice_a = Practice(name=f"Practice A {suffix}", status=PracticeStatus.active)
        practice_b = Practice(name=f"Practice B {suffix}", status=PracticeStatus.active)
        session.add_all([practice_a, practice_b])
        await session.flush()

        clinician_a = User(
            practice_id=practice_a.id,
            email=f"clinician-a-{suffix}@seed.docflow.test",
            full_name="Clinician A",
            role=UserRole.clinician,
        )
        clinician_b = User(
            practice_id=practice_b.id,
            email=f"clinician-b-{suffix}@seed.docflow.test",
            full_name="Clinician B",
            role=UserRole.clinician,
        )
        session.add_all([clinician_a, clinician_b])
        await session.flush()

        result = TwoPractices(
            practice_a_id=practice_a.id,
            practice_b_id=practice_b.id,
            clinician_a_id=clinician_a.id,
            clinician_b_id=clinician_b.id,
        )

    return result
