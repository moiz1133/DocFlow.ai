"""ConsentService.assert_retention_allowed / assert_training_allowed —
the Phase 7 consent gate. Uses the restricted app_sessionmaker (RLS
actually applies to it, unlike admin_sessionmaker) with set_tenant, the
same pattern tests/test_tenant_isolation.py uses.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.session import set_tenant
from app.models import Consent, EncounterSession
from app.models.enums import ConsentType, SessionStatus
from app.security.consent import BlanketRetentionNotAllowedError, ConsentService
from tests.conftest import TwoPractices


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
        "LOCAL_ENCRYPTION_KEY": get_settings().LOCAL_ENCRYPTION_KEY,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


async def _seed_session(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    clinician_id: uuid.UUID,
) -> uuid.UUID:
    """Consent.session_id has a real FK to sessions.id — a session-scoped
    Consent row needs an actual EncounterSession to point at, not just
    any uuid4().
    """
    async with admin_sessionmaker() as session, session.begin():
        encounter = EncounterSession(
            practice_id=practice_id, clinician_id=clinician_id, status=SessionStatus.complete
        )
        session.add(encounter)
        await session.flush()
        return encounter.id


async def _grant_consent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    session_id: uuid.UUID | None,
    consent_type: ConsentType,
    granted: bool = True,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            Consent(
                practice_id=practice_id,
                session_id=session_id,
                consent_type=consent_type,
                granted=granted,
                granted_by="test-harness",
            )
        )


async def test_none_policy_never_retains_even_with_consent_on_file(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    session_id = await _seed_session(
        admin_sessionmaker, practice_id=practice_id, clinician_id=two_practices.clinician_a_id
    )
    await _grant_consent(
        admin_sessionmaker,
        practice_id=practice_id,
        session_id=session_id,
        consent_type=ConsentType.retention,
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_retention_allowed(
            db, session_id=session_id, settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="none")
        )
    assert allowed is False


async def test_always_policy_without_opt_in_raises(
    app_sessionmaker: async_sessionmaker[AsyncSession], two_practices: TwoPractices
) -> None:
    practice_id = two_practices.practice_a_id
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        with pytest.raises(BlanketRetentionNotAllowedError):
            await ConsentService.assert_retention_allowed(
                db,
                session_id=uuid.uuid4(),
                settings=_settings(
                    TRANSCRIPT_RETENTION_DEFAULT="always", ALLOW_BLANKET_RETENTION=False
                ),
            )


async def test_always_policy_with_opt_in_retains_unconditionally(
    app_sessionmaker: async_sessionmaker[AsyncSession], two_practices: TwoPractices
) -> None:
    practice_id = two_practices.practice_a_id
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_retention_allowed(
            db,
            session_id=uuid.uuid4(),  # no consent seeded at all
            settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="always", ALLOW_BLANKET_RETENTION=True),
        )
    assert allowed is True


async def test_consented_policy_without_consent_denies(
    app_sessionmaker: async_sessionmaker[AsyncSession], two_practices: TwoPractices
) -> None:
    practice_id = two_practices.practice_a_id
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_retention_allowed(
            db,
            session_id=uuid.uuid4(),
            settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="consented"),
        )
    assert allowed is False


async def test_consented_policy_with_session_scoped_consent_allows(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    session_id = await _seed_session(
        admin_sessionmaker, practice_id=practice_id, clinician_id=two_practices.clinician_a_id
    )
    await _grant_consent(
        admin_sessionmaker,
        practice_id=practice_id,
        session_id=session_id,
        consent_type=ConsentType.retention,
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_retention_allowed(
            db, session_id=session_id, settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="consented")
        )
    assert allowed is True


async def test_consented_policy_with_practice_wide_consent_allows_any_session(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    await _grant_consent(
        admin_sessionmaker,
        practice_id=practice_id,
        session_id=None,  # practice-wide
        consent_type=ConsentType.recording,
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_retention_allowed(
            db,
            session_id=uuid.uuid4(),  # an entirely different, unrelated session
            settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="consented"),
        )
    assert allowed is True


async def test_consented_policy_ignores_another_practices_consent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_a_id = two_practices.practice_a_id
    practice_b_id = two_practices.practice_b_id
    await _grant_consent(
        admin_sessionmaker,
        practice_id=practice_b_id,
        session_id=None,
        consent_type=ConsentType.retention,
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_a_id)
        allowed = await ConsentService.assert_retention_allowed(
            db,
            session_id=uuid.uuid4(),
            settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="consented"),
        )
    assert allowed is False  # RLS hides practice B's consent row entirely


async def test_ungranted_consent_does_not_count(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    session_id = await _seed_session(
        admin_sessionmaker, practice_id=practice_id, clinician_id=two_practices.clinician_a_id
    )
    await _grant_consent(
        admin_sessionmaker,
        practice_id=practice_id,
        session_id=session_id,
        consent_type=ConsentType.retention,
        granted=False,
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_retention_allowed(
            db, session_id=session_id, settings=_settings(TRANSCRIPT_RETENTION_DEFAULT="consented")
        )
    assert allowed is False


async def test_training_consent_denied_without_explicit_grant(
    app_sessionmaker: async_sessionmaker[AsyncSession], two_practices: TwoPractices
) -> None:
    practice_id = two_practices.practice_a_id
    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_training_allowed(
            db, session_id=None, practice_id=practice_id
        )
    assert allowed is False


async def test_training_consent_allowed_with_explicit_grant(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    app_sessionmaker: async_sessionmaker[AsyncSession],
    two_practices: TwoPractices,
) -> None:
    practice_id = two_practices.practice_a_id
    await _grant_consent(
        admin_sessionmaker,
        practice_id=practice_id,
        session_id=None,
        consent_type=ConsentType.training,
    )

    async with app_sessionmaker() as db, db.begin():
        await set_tenant(db, practice_id)
        allowed = await ConsentService.assert_training_allowed(
            db, session_id=None, practice_id=practice_id
        )
    assert allowed is True
