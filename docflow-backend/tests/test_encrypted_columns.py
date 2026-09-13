"""End-to-end encryption-at-rest tests: write PHI through the ORM, read
the RAW column value back (bypassing the decrypting TypeDecorator) and
confirm it is genuinely ciphertext, then confirm the ORM still reads the
correct plaintext back. Also the DB-level tamper-detection case (a
byte-flip in stored ciphertext surfaces as a decryption error on read,
not silently wrong data).

Uses app_sessionmaker (RLS-scoped, like production) to write and
admin_sessionmaker (bypasses RLS, superuser) only for the raw-column
peek and the deliberate tamper — the same division of responsibility
tests/test_tenant_isolation.py uses.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.session import set_tenant
from app.models import EncounterSession, Note, Transcript, User
from app.models.enums import SessionStatus, UserRole
from app.security.encryption import DecryptionError, blob_key_version, is_encrypted_blob
from tests.conftest import TwoPractices


async def _seed_session(
    app_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    clinician_id: uuid.UUID,
) -> uuid.UUID:
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        encounter = EncounterSession(
            practice_id=practice_id, clinician_id=clinician_id, status=SessionStatus.complete
        )
        db.add(encounter)
        await db.flush()
        return encounter.id


async def _raw_column(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    table: str,
    column: str,
    row_id: uuid.UUID,
) -> str:
    # table/column are always hardcoded literals from this file's own
    # call sites, never external input — safe to interpolate.
    async with admin_sessionmaker() as raw:
        result = await raw.execute(
            text(f'SELECT "{column}" FROM {table} WHERE id = :id'), {"id": row_id}
        )
        value: str = result.scalar_one()
        return value


async def test_transcript_content_and_segments_are_ciphertext_at_rest(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    session_id = await _seed_session(
        app_sessionmaker, practice_id=practice_id, clinician_id=two_practices.clinician_a_id
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        transcript = Transcript(
            practice_id=practice_id,
            session_id=session_id,
            content="patient reports chest pain and shortness of breath",
            segments=[{"speaker": None, "start": 0.0, "end": 1.0, "text": "chest pain"}],
            is_retained=True,
        )
        db.add(transcript)
        await db.flush()
        transcript_id = transcript.id

    raw_content = await _raw_column(
        admin_sessionmaker, table="transcripts", column="content", row_id=transcript_id
    )
    raw_segments = await _raw_column(
        admin_sessionmaker, table="transcripts", column="segments", row_id=transcript_id
    )

    assert "chest pain" not in raw_content
    assert "chest pain" not in raw_segments
    assert is_encrypted_blob(raw_content)
    assert is_encrypted_blob(raw_segments)
    assert blob_key_version(raw_content) == get_settings().ENCRYPTION_KEY_VERSION

    # The ORM still reads the correct plaintext back, transparently.
    async with app_sessionmaker() as db2, db2.begin():
        await set_tenant(db2, practice_id)
        fetched = await db2.get(Transcript, transcript_id)
        assert fetched is not None
        assert fetched.content == "patient reports chest pain and shortness of breath"
        assert fetched.segments == [
            {"speaker": None, "start": 0.0, "end": 1.0, "text": "chest pain"}
        ]


async def test_note_sections_are_ciphertext_at_rest(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    session_id = await _seed_session(
        app_sessionmaker, practice_id=practice_id, clinician_id=two_practices.clinician_a_id
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        note = Note(
            practice_id=practice_id,
            session_id=session_id,
            subjective="reports tension headache",
            objective="BP 120/80",
            assessment="tension headache",
            plan="rest and fluids",
            full_text="SOAP note text",
            is_retained=True,
        )
        db.add(note)
        await db.flush()
        note_id = note.id

    raw_subjective = await _raw_column(
        admin_sessionmaker, table="notes", column="subjective", row_id=note_id
    )
    assert "tension headache" not in raw_subjective
    assert is_encrypted_blob(raw_subjective)

    async with app_sessionmaker() as db2, db2.begin():
        await set_tenant(db2, practice_id)
        fetched = await db2.get(Note, note_id)
        assert fetched is not None
        assert fetched.subjective == "reports tension headache"


async def test_session_patient_ref_is_ciphertext_at_rest(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        encounter = EncounterSession(
            practice_id=practice_id,
            clinician_id=two_practices.clinician_a_id,
            status=SessionStatus.created,
            patient_ref="Rm 4, 2pm follow-up",
        )
        db.add(encounter)
        await db.flush()
        session_id = encounter.id

    raw_patient_ref = await _raw_column(
        admin_sessionmaker, table="sessions", column="patient_ref", row_id=session_id
    )
    assert "Rm 4" not in raw_patient_ref
    assert is_encrypted_blob(raw_patient_ref)


async def test_user_mfa_secret_is_ciphertext_at_rest(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        suffix = uuid.uuid4().hex[:8]
        user = User(
            practice_id=practice_id,
            email=f"mfa-user-{suffix}@example.com",
            full_name="MFA Test User",
            role=UserRole.clinician,
            mfa_secret="JBSWY3DPEHPK3PXP",
        )
        db.add(user)
        await db.flush()
        user_id = user.id

    raw_secret = await _raw_column(
        admin_sessionmaker, table="users", column="mfa_secret", row_id=user_id
    )
    assert "JBSWY3DPEHPK3PXP" not in raw_secret
    assert is_encrypted_blob(raw_secret)


async def test_tampered_ciphertext_raises_on_read(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    session_id = await _seed_session(
        app_sessionmaker, practice_id=practice_id, clinician_id=two_practices.clinician_a_id
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        transcript = Transcript(
            practice_id=practice_id,
            session_id=session_id,
            content="patient reports chest pain",
            segments=[],
            is_retained=True,
        )
        db.add(transcript)
        await db.flush()
        transcript_id = transcript.id

    raw_content = await _raw_column(
        admin_sessionmaker, table="transcripts", column="content", row_id=transcript_id
    )
    tampered = raw_content[:-4] + ("A" if raw_content[-4] != "A" else "B") + raw_content[-3:]

    async with admin_sessionmaker() as raw, raw.begin():
        await raw.execute(
            text("UPDATE transcripts SET content = :val WHERE id = :id"),
            {"val": tampered, "id": transcript_id},
        )

    try:
        async with app_sessionmaker() as db2, db2.begin():
            await set_tenant(db2, practice_id)
            with pytest.raises(DecryptionError):
                await db2.get(Transcript, transcript_id)
    finally:
        # This row's `content` is permanently corrupted on purpose — left
        # in place, it would break any later test that blanket-decrypts
        # every row in this column (e.g. test_migrations.py's downgrade,
        # or scripts/rotate_encryption_key.py), since the test database
        # is shared for the whole session. Clean it up rather than leak
        # deliberately-broken data past this test.
        async with admin_sessionmaker() as cleanup, cleanup.begin():
            await cleanup.execute(
                text("DELETE FROM transcripts WHERE id = :id"), {"id": transcript_id}
            )
