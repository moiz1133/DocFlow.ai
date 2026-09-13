"""Scheduled retention-purge job (Phase 8) — app/worker/tasks.py's
purge_expired_data_task. Seeds rows directly via admin_sessionmaker with
backdated updated_at/expires_at timestamps (TimestampMixin's
server_default only applies when no explicit value is given at INSERT,
so this is a legitimate way to simulate "past its TTL" without waiting).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.models import AuditLog, EncounterSession, Note, RefreshToken, Transcript
from app.models.enums import SessionStatus
from app.worker.tasks import purge_expired_data_task
from tests.conftest import TwoPractices

_OLD = datetime.now(UTC) - timedelta(hours=48)
_RECENT = datetime.now(UTC) - timedelta(minutes=5)


def _tight_purge_settings(**overrides: object) -> Settings:
    base = get_settings().model_copy(
        update={
            "PURGE_UNRETAINED_AFTER": "24h",
            "PURGE_ORPHAN_SESSION_AFTER": "24h",
            "PURGE_DRY_RUN": True,
        }
    )
    return base.model_copy(update=overrides)


async def _seed_session(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    clinician_id: uuid.UUID,
    status: SessionStatus = SessionStatus.complete,
    updated_at: datetime = _RECENT,
) -> uuid.UUID:
    async with admin_sessionmaker() as session, session.begin():
        encounter = EncounterSession(
            practice_id=practice_id, clinician_id=clinician_id, status=status
        )
        session.add(encounter)
        await session.flush()
        encounter.updated_at = updated_at
        await session.flush()
        return encounter.id


async def _seed_transcript(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    session_id: uuid.UUID,
    is_retained: bool,
    updated_at: datetime,
) -> uuid.UUID:
    async with admin_sessionmaker() as session, session.begin():
        transcript = Transcript(
            practice_id=practice_id,
            session_id=session_id,
            content="some content" if is_retained else "",
            segments=[],
            is_retained=is_retained,
        )
        session.add(transcript)
        await session.flush()
        transcript.updated_at = updated_at
        await session.flush()
        return transcript.id


async def _run_purge(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Any:
    import app.worker.tasks as tasks_module

    settings = _tight_purge_settings(**overrides)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)
    result = purge_expired_data_task.delay()
    return result.get()


async def test_dry_run_reports_but_does_not_delete_unretained_transcript_past_ttl(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
    )
    transcript_id = await _seed_transcript(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        session_id=session_id,
        is_retained=False,
        updated_at=_OLD,
    )

    summary = await _run_purge(monkeypatch, PURGE_DRY_RUN=True)

    assert summary["dry_run"] is True
    assert summary["transcripts_deleted"] >= 1

    async with admin_sessionmaker() as session:
        result = await session.execute(select(Transcript.id).where(Transcript.id == transcript_id))
        assert result.scalar_one_or_none() == transcript_id  # still there — dry run only


async def test_real_mode_deletes_unretained_transcript_past_ttl(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
    )
    transcript_id = await _seed_transcript(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        session_id=session_id,
        is_retained=False,
        updated_at=_OLD,
    )

    summary = await _run_purge(monkeypatch, PURGE_DRY_RUN=False)
    assert summary["dry_run"] is False

    async with admin_sessionmaker() as session:
        result = await session.execute(select(Transcript.id).where(Transcript.id == transcript_id))
        assert result.scalar_one_or_none() is None  # actually deleted


async def test_retained_transcript_past_ttl_is_never_purged(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
    )
    transcript_id = await _seed_transcript(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        session_id=session_id,
        is_retained=True,
        updated_at=_OLD,
    )

    await _run_purge(monkeypatch, PURGE_DRY_RUN=False)

    async with admin_sessionmaker() as session:
        result = await session.execute(select(Transcript.id).where(Transcript.id == transcript_id))
        # Retained — untouched regardless of age.
        assert result.scalar_one_or_none() == transcript_id


async def test_unretained_transcript_within_ttl_is_never_purged(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
    )
    transcript_id = await _seed_transcript(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        session_id=session_id,
        is_retained=False,
        updated_at=_RECENT,
    )

    await _run_purge(monkeypatch, PURGE_DRY_RUN=False)

    async with admin_sessionmaker() as session:
        result = await session.execute(select(Transcript.id).where(Transcript.id == transcript_id))
        assert result.scalar_one_or_none() == transcript_id  # too recent — untouched


async def test_orphan_error_session_past_threshold_is_purged(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = await _seed_session(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
        status=SessionStatus.error,
        updated_at=_OLD,
    )

    summary = await _run_purge(monkeypatch, PURGE_DRY_RUN=False)
    assert summary["sessions_deleted"] >= 1

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(EncounterSession.id).where(EncounterSession.id == session_id)
        )
        assert result.scalar_one_or_none() is None


async def test_orphan_session_with_retained_note_is_never_purged(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive guard: even an error-status session old enough to
    otherwise qualify must not be purged if it somehow still carries
    RETAINED clinical content — see app/worker/tasks.py's
    _purge_practice docstring on why this check exists despite Note/
    Transcript already CASCADE-deleting with their session.
    """
    session_id = await _seed_session(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
        status=SessionStatus.error,
        updated_at=_OLD,
    )
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            Note(
                practice_id=two_practices.practice_a_id,
                session_id=session_id,
                subjective="retained content",
                is_retained=True,
            )
        )

    await _run_purge(monkeypatch, PURGE_DRY_RUN=False)

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(EncounterSession.id).where(EncounterSession.id == session_id)
        )
        assert result.scalar_one_or_none() == session_id


async def test_expired_refresh_token_is_purged_but_valid_one_is_not(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    async with admin_sessionmaker() as session, session.begin():
        expired = RefreshToken(
            practice_id=two_practices.practice_a_id,
            user_id=two_practices.clinician_a_id,
            jti=uuid.uuid4().hex,
            expires_at=now - timedelta(days=1),
        )
        # Revoked but NOT yet expired — must survive the purge (see
        # app/worker/tasks.py's docstring on reuse-detection safety).
        revoked_not_expired = RefreshToken(
            practice_id=two_practices.practice_a_id,
            user_id=two_practices.clinician_a_id,
            jti=uuid.uuid4().hex,
            expires_at=now + timedelta(days=6),
            revoked=True,
        )
        valid = RefreshToken(
            practice_id=two_practices.practice_a_id,
            user_id=two_practices.clinician_a_id,
            jti=uuid.uuid4().hex,
            expires_at=now + timedelta(days=7),
        )
        session.add_all([expired, revoked_not_expired, valid])
        await session.flush()
        expired_id, revoked_id, valid_id = expired.id, revoked_not_expired.id, valid.id

    await _run_purge(monkeypatch, PURGE_DRY_RUN=False)

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(RefreshToken.id).where(RefreshToken.id.in_([expired_id, revoked_id, valid_id]))
        )
        remaining_ids = set(result.scalars().all())
    assert remaining_ids == {revoked_id, valid_id}


async def test_purge_never_touches_audit_logs(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with admin_sessionmaker() as session:
        before_ids = set((await session.execute(select(AuditLog.id))).scalars().all())

    await _run_purge(monkeypatch, PURGE_DRY_RUN=False)

    async with admin_sessionmaker() as session:
        after_result = await session.execute(select(AuditLog))
        after_rows = after_result.scalars().all()
    after_ids = {row.id for row in after_rows}

    # Every pre-existing audit row is still present, untouched.
    assert before_ids.issubset(after_ids)
    # New rows are only ever the purge run's own PHI-free summaries.
    new_rows = [row for row in after_rows if row.id not in before_ids]
    assert new_rows
    assert all(row.action.value == "purge_completed" for row in new_rows)
    assert all(row.resource_type == "purge" for row in new_rows)
    assert all(row.actor_user_id is None for row in new_rows)


async def test_purge_writes_a_phi_free_summary_audit_row_per_practice(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _run_purge(monkeypatch, PURGE_DRY_RUN=True)

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.action == "purge_completed",
                AuditLog.practice_id == two_practices.practice_a_id,
            )
        )
        rows = result.scalars().all()

    assert len(rows) >= 1
    metadata = rows[-1].metadata_
    assert metadata is not None
    assert set(metadata.keys()) == {
        "dry_run",
        "transcripts_deleted",
        "notes_deleted",
        "sessions_deleted",
        "refresh_tokens_deleted",
    }
    assert all(isinstance(v, bool | int) for v in metadata.values())
