"""audit_logs rows can be created and read, but never changed or erased.

Each test explicitly rolls back after the expected failure rather than
relying on `session.begin()`'s context-manager to commit on clean exit:
once flush() (or a raw UPDATE) has raised, the session/transaction is left
in a failed state, and letting the context manager attempt a commit there
would raise a second, unrelated error instead of exercising the one this
test is actually about.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import set_tenant
from app.models.audit_log import AuditLog, AuditLogImmutableError
from app.models.enums import AuditAction
from tests.conftest import TwoPractices


async def _insert_audit_log(
    session: AsyncSession, practice_id: uuid.UUID, resource_type: str = "session"
) -> AuditLog:
    entry = AuditLog(
        practice_id=practice_id,
        actor_user_id=None,
        action=AuditAction.create,
        resource_type=resource_type,
    )
    session.add(entry)
    await session.flush()
    return entry


async def test_update_is_blocked_at_the_orm_level(
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    async with app_sessionmaker() as session:
        await session.begin()
        await set_tenant(session, two_practices.practice_a_id)
        entry = await _insert_audit_log(session, two_practices.practice_a_id)

        entry.resource_type = "tampered"
        with pytest.raises(AuditLogImmutableError):
            await session.flush()

        await session.rollback()


async def test_delete_is_blocked_at_the_orm_level(
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    async with app_sessionmaker() as session:
        await session.begin()
        await set_tenant(session, two_practices.practice_a_id)
        entry = await _insert_audit_log(session, two_practices.practice_a_id)

        await session.delete(entry)
        with pytest.raises(AuditLogImmutableError):
            await session.flush()

        await session.rollback()


async def test_update_is_also_blocked_at_the_database_level(
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    """Belt-and-suspenders: even raw SQL (bypassing the ORM event) is
    blocked, because the app role has UPDATE/DELETE revoked on audit_logs.
    """
    async with app_sessionmaker() as session:
        await session.begin()
        await set_tenant(session, two_practices.practice_a_id)
        entry = await _insert_audit_log(session, two_practices.practice_a_id)
        entry_id = entry.id
        await session.commit()

    async with app_sessionmaker() as session:
        await session.begin()
        await set_tenant(session, two_practices.practice_a_id)
        with pytest.raises(DBAPIError, match="permission denied"):
            await session.execute(
                text("UPDATE audit_logs SET resource_type = 'tampered' WHERE id = :id"),
                {"id": entry_id},
            )
        await session.rollback()
