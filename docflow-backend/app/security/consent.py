"""The consent gate: the single authority deciding whether transcript/note
PHI may be persisted (Phase 7). Replaces the per-caller discretion Phases
5 and 6 each used to resolve this independently
(app/services/transcription_service.py's old `_resolve_retention`, and
Phase 6's note persistence, which didn't gate retention at all) — both
now call ConsentService.assert_retention_allowed instead.

Policy is Settings.TRANSCRIPT_RETENTION_DEFAULT:
- "none": never retain, full stop.
- "consented" (default): retain only if a granted Consent (type
  `recording` or `retention`) exists for this session, or practice-wide
  (session_id IS NULL).
- "always": retain unconditionally — but only for a practice that has
  signed a blanket retention agreement, which is what the
  ALLOW_BLANKET_RETENTION opt-in is meant to represent operationally.
  RISK: this bypasses the per-session consent check entirely; it exists
  for practices under a different (BAA-level) retention agreement, not
  as a convenient default. See app/security/startup_checks.py, which
  additionally refuses to boot with PHI_MODE=real, this policy, and no
  opt-in.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Consent
from app.models.enums import ConsentType


class BlanketRetentionNotAllowedError(Exception):
    """TRANSCRIPT_RETENTION_DEFAULT=always without the explicit
    ALLOW_BLANKET_RETENTION opt-in — a configuration error, not a normal
    per-session denial (which returns False rather than raising).
    """


class ConsentService:
    """Stateless — a namespace rather than an instance, so call sites
    read as `ConsentService.assert_retention_allowed(...)`, matching how
    the spec names this authority.
    """

    @staticmethod
    async def assert_retention_allowed(
        db: AsyncSession, *, session_id: uuid.UUID, settings: Settings | None = None
    ) -> bool:
        """Returns whether transcript/note PHI for this session may be
        persisted. Never raises for an ordinary "no consent on file"
        denial — that's an expected, common outcome the caller handles
        by storing a minimal stub (see
        app/services/transcription_service.py's persist_transcript and
        app/services/note_service.py's _persist_success/_persist_degraded).
        Only raises for the "always" + no opt-in misconfiguration, which
        is a deployment error, not a per-session decision.
        """
        settings = settings or get_settings()
        policy = settings.TRANSCRIPT_RETENTION_DEFAULT

        if policy == "none":
            return False

        if policy == "always":
            if not settings.ALLOW_BLANKET_RETENTION:
                raise BlanketRetentionNotAllowedError(
                    "TRANSCRIPT_RETENTION_DEFAULT=always requires "
                    "ALLOW_BLANKET_RETENTION=true — see app/security/consent.py"
                )
            return True

        # "consented": an explicit granted consent, scoped to this
        # session or practice-wide, is required — retention is never
        # implied merely by a transcript/note existing.
        result = await db.execute(
            select(Consent.id).where(
                Consent.consent_type.in_([ConsentType.recording, ConsentType.retention]),
                Consent.granted.is_(True),
                (Consent.session_id == session_id) | (Consent.session_id.is_(None)),
            )
        )
        return result.first() is not None

    @staticmethod
    async def assert_training_allowed(
        db: AsyncSession, *, session_id: uuid.UUID | None, practice_id: uuid.UUID
    ) -> bool:
        """Whether PHI from this session (or practice-wide) may be sent
        down any model-training/improvement path. No such path exists in
        this codebase yet — this is the belt-and-suspenders check a
        future one MUST call before ever doing so, alongside (never
        instead of) the vendor BAA/Zero-Data-Retention guarantee. Always
        requires an explicit, granted Consent(training) — there is no
        "always"/"none" policy shortcut for training data, unlike
        retention: training consent is never assumed.
        """
        result = await db.execute(
            select(Consent.id).where(
                Consent.practice_id == practice_id,
                Consent.consent_type == ConsentType.training,
                Consent.granted.is_(True),
                (Consent.session_id == session_id) | (Consent.session_id.is_(None)),
            )
        )
        return result.first() is not None
