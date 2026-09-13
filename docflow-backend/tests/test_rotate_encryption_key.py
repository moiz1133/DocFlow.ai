"""Tests for scripts/rotate_encryption_key.py against the real test
database. Seeds a row encrypted under a deliberately distinct "old" key
(bypassing the ORM's own encrypt-on-write, which uses this test
session's normal LOCAL_ENCRYPTION_KEY — see tests/conftest.py) so the
rotation's before/after state is fully under this test's control.
"""

import base64
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EncounterSession
from app.models.enums import SessionStatus
from app.security.encryption import blob_key_version, decrypt_value, encrypt_value
from app.security.keys import LocalKeyProvider
from scripts.rotate_encryption_key import rotate
from tests.conftest import TwoPractices

_OLD_VERSION = 101
_NEW_VERSION = 102
_OLD_KEY = b"o" * 32
_NEW_KEY = b"n" * 32
_PLAINTEXT = "patient reports chest pain, pre-rotation"


async def _seed_pre_rotation_transcript(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    clinician_id: uuid.UUID,
) -> uuid.UUID:
    """Inserts a transcript row whose `content` is already a valid
    envelope blob under _OLD_VERSION/_OLD_KEY — via raw SQL, deliberately
    bypassing the ORM's PHIText encryption (which would use this test
    session's own LOCAL_ENCRYPTION_KEY, a different key entirely).
    """
    seed_provider = LocalKeyProvider(keys={_OLD_VERSION: _OLD_KEY}, active_version=_OLD_VERSION)
    content_blob = encrypt_value(_PLAINTEXT, key_provider=seed_provider, key_version=_OLD_VERSION)
    # segments is NOT NULL and also a PHI column the rotation script will
    # walk (see app/security/phi_columns.py) — give it a real encrypted
    # empty-array blob rather than arbitrary text, so rotation processes
    # it the same way it would a real row.
    segments_blob = encrypt_value("[]", key_provider=seed_provider, key_version=_OLD_VERSION)

    async with admin_sessionmaker() as session, session.begin():
        encounter = EncounterSession(
            practice_id=practice_id, clinician_id=clinician_id, status=SessionStatus.complete
        )
        session.add(encounter)
        await session.flush()
        session_id = encounter.id
        await session.execute(
            text(
                "INSERT INTO transcripts (id, practice_id, session_id, content, segments, "
                "is_retained) VALUES (gen_random_uuid(), :practice_id, :session_id, :content, "
                ":segments, true)"
            ),
            {
                "practice_id": practice_id,
                "session_id": session_id,
                "content": content_blob,
                "segments": segments_blob,
            },
        )
    return session_id


async def _raw_content(
    admin_sessionmaker: async_sessionmaker[AsyncSession], *, session_id: uuid.UUID
) -> str:
    async with admin_sessionmaker() as session:
        result = await session.execute(
            text("SELECT content FROM transcripts WHERE session_id = :session_id"),
            {"session_id": session_id},
        )
        value: str = result.scalar_one()
        return value


async def test_rotate_moves_a_row_from_the_old_version_to_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    session_id = await _seed_pre_rotation_transcript(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
    )

    before = await _raw_content(admin_sessionmaker, session_id=session_id)
    assert blob_key_version(before) == _OLD_VERSION

    monkeypatch.setenv("ROTATE_OLD_KEY_VERSION", str(_OLD_VERSION))
    monkeypatch.setenv("ROTATE_OLD_LOCAL_ENCRYPTION_KEY", base64.b64encode(_OLD_KEY).decode())
    monkeypatch.setenv("ROTATE_NEW_KEY_VERSION", str(_NEW_VERSION))
    monkeypatch.setenv("ROTATE_NEW_LOCAL_ENCRYPTION_KEY", base64.b64encode(_NEW_KEY).decode())

    await rotate(dry_run=False)

    after = await _raw_content(admin_sessionmaker, session_id=session_id)
    assert blob_key_version(after) == _NEW_VERSION
    assert after != before

    # The new blob decrypts correctly under the new key alone.
    new_only_provider = LocalKeyProvider(keys={_NEW_VERSION: _NEW_KEY}, active_version=_NEW_VERSION)
    assert decrypt_value(after, key_provider=new_only_provider) == _PLAINTEXT


async def test_dry_run_leaves_rows_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    session_id = await _seed_pre_rotation_transcript(
        admin_sessionmaker,
        practice_id=two_practices.practice_a_id,
        clinician_id=two_practices.clinician_a_id,
    )
    before = await _raw_content(admin_sessionmaker, session_id=session_id)

    monkeypatch.setenv("ROTATE_OLD_KEY_VERSION", str(_OLD_VERSION))
    monkeypatch.setenv("ROTATE_OLD_LOCAL_ENCRYPTION_KEY", base64.b64encode(_OLD_KEY).decode())
    monkeypatch.setenv("ROTATE_NEW_KEY_VERSION", str(_NEW_VERSION))
    monkeypatch.setenv("ROTATE_NEW_LOCAL_ENCRYPTION_KEY", base64.b64encode(_NEW_KEY).decode())

    await rotate(dry_run=True)

    after = await _raw_content(admin_sessionmaker, session_id=session_id)
    assert after == before
    assert blob_key_version(after) == _OLD_VERSION


async def test_both_versions_decrypt_through_a_dual_key_provider_mid_transition() -> None:
    """A provider holding both the pre- and post-rotation key (as an
    operator would keep, briefly, during a real rotation window) can
    decrypt values on either version — old ones not yet processed, new
    ones already rotated — simultaneously. See
    app/security/keys.py's LocalKeyProvider.
    """
    transition_provider = LocalKeyProvider(
        keys={_OLD_VERSION: _OLD_KEY, _NEW_VERSION: _NEW_KEY}, active_version=_NEW_VERSION
    )
    old_blob = encrypt_value(
        "not yet rotated", key_provider=transition_provider, key_version=_OLD_VERSION
    )
    new_blob = encrypt_value(
        "already rotated", key_provider=transition_provider, key_version=_NEW_VERSION
    )

    assert decrypt_value(old_blob, key_provider=transition_provider) == "not yet rotated"
    assert decrypt_value(new_blob, key_provider=transition_provider) == "already rotated"
