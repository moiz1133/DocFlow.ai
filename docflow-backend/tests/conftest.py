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
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long")
# Point straight at docflow_test (not docflow): app.db.session.get_engine()
# reads APP_DATABASE_URL directly (not through the _with_dbname swap
# below), and it must land on the exact same throwaway database that
# _provisioned_database creates/migrates/drops — otherwise HTTP-level
# tests hitting the real app would write to the dev database while
# fixtures asserting on the DB would read from docflow_test.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://docflow:docflow@localhost:5433/docflow_test"
)
os.environ.setdefault(
    "APP_DATABASE_URL",
    "postgresql+asyncpg://docflow_app:docflow_app_dev_only@localhost:5433/docflow_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("ENV", "dev")
# KEY_PROVIDER defaults to "local", which needs this — a fixed dev/test
# key (not a secret; never used outside this test session and the local
# .env.example) so every test in the session encrypts/decrypts
# consistently. See app/security/keys.py.
os.environ.setdefault("LOCAL_ENCRYPTION_KEY", "TlGtLjbjLbojhmM71ihT1kx4TUJaVXUARSqQqA9gwBY=")

import pytest
import pytest_asyncio
import redis.asyncio as redis
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from httpx_ws.transport import ASGIWebSocketTransport
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


@pytest.fixture(scope="session", autouse=True)
def _celery_eager_mode() -> None:
    """Runs every Celery task inline, synchronously, in the calling
    process — no live worker needed for tests (see app/worker/tasks.py
    and the "vendors=mock, eager Celery" convention this test suite
    follows throughout). task_eager_propagates=True makes an eagerly-run
    task's exception raise directly at the .delay()/.apply() call site
    instead of only being visible via the AsyncResult, which is what
    tests asserting on a failure path expect.
    """
    from app.worker.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


@pytest_asyncio.fixture(autouse=True)
async def _flush_rate_limit_keys() -> AsyncIterator[None]:
    """Rate-limit counters are keyed by (action, client IP), and every
    test request comes from the same fake TestClient IP — without this,
    unrelated tests hitting /login or /refresh many times in one pytest
    session would eventually trip each other's 429s.
    """
    client = redis.from_url(get_settings().REDIS_URL)
    try:
        async for key in client.scan_iter(match="ratelimit:*"):
            await client.delete(key)
        yield
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An httpx client wired directly to the FastAPI app (no real socket)."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def ws_client() -> AsyncClient:
    """Like `client`, but wired through httpx-ws's ASGI transport so
    websocket_connect works too. Runs in-process, on the same event loop
    as everything else in this test session — deliberately not
    starlette.testclient.TestClient, which spins up its own thread and
    event loop and would bind app/db/session.py's @lru_cache engine to a
    *different* loop than the rest of the (session-scoped-loop) suite —
    see this module's docstring on asyncio_default_fixture_loop_scope.

    Deliberately NOT entered here (no `async with`, no yield-based
    teardown): ASGIWebSocketTransport.__aenter__ creates an anyio task
    group bound to the task that entered it, and __aexit__ must run in
    that same task — a pytest-asyncio async-generator fixture's teardown
    phase runs as a separate top-level task from its setup phase, which
    trips anyio's "cancel scope in a different task" check. Callers
    (tests/test_sessions_ws.py's `_connect`) enter and exit this client
    themselves, within one `async with` in the test's own task.
    """
    from app.main import app

    transport = ASGIWebSocketTransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")
