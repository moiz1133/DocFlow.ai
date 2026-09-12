"""Row-Level Security actually isolates tenants, not just on paper.

Every query here runs through app_sessionmaker — the restricted,
non-superuser, NOBYPASSRLS role — because that's the only role RLS
policies apply to at all (the admin/migration role is a superuser in
local dev and bypasses RLS unconditionally).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import set_tenant
from app.models import EncounterSession, Note
from app.models.enums import SessionStatus
from tests.conftest import TwoPractices


async def _seed_session_and_note(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    practice_id: uuid.UUID,
    clinician_id: uuid.UUID,
    patient_ref: str,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        encounter = EncounterSession(
            practice_id=practice_id,
            clinician_id=clinician_id,
            status=SessionStatus.complete,
            patient_ref=patient_ref,
        )
        session.add(encounter)
        await session.flush()

        session.add(
            Note(
                practice_id=practice_id,
                session_id=encounter.id,
                subjective=f"note for {patient_ref}",
            )
        )


async def test_cross_tenant_reads_are_blocked(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    await _seed_session_and_note(
        admin_sessionmaker,
        two_practices.practice_a_id,
        two_practices.clinician_a_id,
        "practice-a-patient",
    )
    await _seed_session_and_note(
        admin_sessionmaker,
        two_practices.practice_b_id,
        two_practices.clinician_b_id,
        "practice-b-patient",
    )

    async with app_sessionmaker() as session, session.begin():
        await set_tenant(session, two_practices.practice_a_id)

        visible_sessions = (await session.execute(select(EncounterSession))).scalars().all()
        visible_notes = (await session.execute(select(Note))).scalars().all()

    assert {s.practice_id for s in visible_sessions} == {two_practices.practice_a_id}
    assert {n.practice_id for n in visible_notes} == {two_practices.practice_a_id}
    assert len(visible_sessions) == 1
    assert len(visible_notes) == 1


async def test_switching_tenant_switches_visibility(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    await _seed_session_and_note(
        admin_sessionmaker,
        two_practices.practice_a_id,
        two_practices.clinician_a_id,
        "practice-a-patient",
    )
    await _seed_session_and_note(
        admin_sessionmaker,
        two_practices.practice_b_id,
        two_practices.clinician_b_id,
        "practice-b-patient",
    )

    async with app_sessionmaker() as session, session.begin():
        await set_tenant(session, two_practices.practice_b_id)
        visible = (await session.execute(select(EncounterSession))).scalars().all()

    assert {s.practice_id for s in visible} == {two_practices.practice_b_id}


async def test_no_tenant_set_sees_nothing(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    """Fail-closed: with no app.current_practice_id set, no rows are visible."""
    await _seed_session_and_note(
        admin_sessionmaker,
        two_practices.practice_a_id,
        two_practices.clinician_a_id,
        "practice-a-patient",
    )

    async with app_sessionmaker() as session, session.begin():
        visible = (await session.execute(select(EncounterSession))).scalars().all()

    assert visible == []
