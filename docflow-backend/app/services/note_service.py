"""Transcript -> scrub -> SOAP note -> persist pipeline (Phase 6),
now gated by the Phase 7 consent authority and with PHI read/write
access fully audited.

A plain async callable taking only plain values (uuids), never a
FastAPI Request/Session — so Phase 8 can move this to a Celery task with
no rewrite; app/api/notes.py's route just awaits it directly for now.
Opens its own short-lived, tenant-scoped DB transactions via
app.services.transcription_service.tenant_session, the same helper the
Phase 5 WS handler uses for the same reason (a caller outside an HTTP
request can't reuse a request-scoped session) — and for the same benefit
Phase 5 relies on: a concurrent GET /v1/sessions/{id} sees status
transitions (generating -> complete/complete_degraded) as they commit,
not only once the whole pipeline finishes.

Nothing here imports a vendor SDK; every vendor interaction goes through
the Phase 6 NoteGenerator interface (app/notes/base.py).
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.config import get_settings
from app.models import EncounterSession, Note, Transcript
from app.models.enums import AuditAction, NoteStatus, SessionStatus
from app.notes.base import NoteContext, NoteGenerationError, SoapNote
from app.notes.factory import get_note_generator
from app.notes.scrub import HeuristicScrubber
from app.security.consent import ConsentService
from app.services.audit_service import RequestMeta
from app.services.transcription_service import record_ingestion_event, tenant_session

logger = logging.getLogger(__name__)

_scrubber = HeuristicScrubber()


class NoteServiceError(Exception):
    """Base for errors app/api/notes.py maps to an HTTP response."""


class SessionNotFoundError(NoteServiceError):
    """No session with this id in the caller's practice."""


class TranscriptNotReadyError(NoteServiceError):
    """The session exists but has no transcript yet (Phase 5 hasn't
    produced one) — nothing to generate a note from.
    """


@dataclass
class NoteGenerationResult:
    """`note` is the persisted row — its content columns may be gated
    empty when is_retained is False (see _persist_success/
    _persist_degraded below). The other fields are always the REAL
    in-memory content regardless of retention, mirroring
    app/services/transcription_service.py's persist_transcript
    convention (and app/api/sessions.py's finalize route, which builds
    its response from the in-memory result rather than the persisted
    Transcript row) — app/api/notes.py's route must build NoteOut from
    THESE fields, not from `note`'s columns.
    """

    note: Note
    degraded: bool
    is_retained: bool
    subjective: str | None
    objective: str | None
    assessment: str | None
    plan: str | None
    full_text: str
    provider: str
    model: str | None


async def generate_note_for_session(
    *,
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_meta: RequestMeta | None = None,
) -> NoteGenerationResult:
    """`request_meta` is plain data (ip_address/user_agent), not a
    FastAPI Request — see app/services/audit_service.py's RequestMeta —
    so this function stays a plain callable Phase 8 can hand to a Celery
    task unchanged, per the module docstring.
    """
    settings = get_settings()

    async with tenant_session(practice_id) as db:
        encounter = await db.get(EncounterSession, session_id)
        if encounter is None:
            raise SessionNotFoundError(f"session {session_id} not found")

        transcript_row = (
            await db.execute(select(Transcript).where(Transcript.session_id == session_id))
        ).scalar_one_or_none()
        if transcript_row is None:
            raise TranscriptNotReadyError(f"session {session_id} has no transcript yet")

        # Read while still tenant-scoped; used below after this
        # transaction has already committed and closed.
        transcript_text = transcript_row.content

        # PHI READ, audited once for this logical operation (not once
        # per decrypted column — see app/services/audit_service.py).
        await record_ingestion_event(
            db,
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            action=AuditAction.read,
            resource_type="transcript",
            resource_id=transcript_row.id,
            metadata={"purpose": "note_generation"},
            request_meta=request_meta,
        )

        encounter.status = SessionStatus.generating
        await db.flush()

    cleaned_text = (
        _scrubber.scrub(transcript_text) if settings.NOTE_SCRUB_ENABLED else transcript_text
    )

    generator = get_note_generator()
    context = NoteContext(specialty="primary_care")

    try:
        soap = await generator.generate_soap(cleaned_text, context)
    except NoteGenerationError as exc:
        return await _persist_degraded(
            session_id=session_id,
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            transcript_text=transcript_text,
            provider=generator.provider_name,
            retry_count=generator.last_retry_count,
            reason=type(exc).__name__,
            request_meta=request_meta,
        )

    return await _persist_success(
        session_id=session_id,
        practice_id=practice_id,
        actor_user_id=actor_user_id,
        soap=soap,
        retry_count=generator.last_retry_count,
        request_meta=request_meta,
    )


async def _persist_success(
    *,
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    soap: SoapNote,
    retry_count: int,
    request_meta: RequestMeta | None = None,
) -> NoteGenerationResult:
    settings = get_settings()
    async with tenant_session(practice_id) as db:
        is_retained = await ConsentService.assert_retention_allowed(db, session_id=session_id)

        note = Note(
            practice_id=practice_id,
            session_id=session_id,
            format="soap",
            subjective=soap.subjective if is_retained else None,
            objective=soap.objective if is_retained else None,
            assessment=soap.assessment if is_retained else None,
            plan=soap.plan if is_retained else None,
            full_text=soap.full_text if is_retained else None,
            status=NoteStatus.draft,
            version=1,
            prompt_version=settings.NOTE_PROMPT_VERSION,
            provider=soap.provider,
            model=soap.model,
            degraded=False,
            is_retained=is_retained,
        )
        db.add(note)
        await db.flush()

        encounter = await db.get(EncounterSession, session_id)
        assert encounter is not None  # fetched successfully moments ago, same tenant scope
        encounter.status = SessionStatus.complete
        await db.flush()

        await record_ingestion_event(
            db,
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            action=AuditAction.create,
            resource_type="note",
            resource_id=note.id,
            metadata={
                "provider": soap.provider,
                "model": soap.model,
                "prompt_version": settings.NOTE_PROMPT_VERSION,
                "retry_count": retry_count,
                "degraded": False,
                "is_retained": is_retained,
            },
            request_meta=request_meta,
        )
        if not is_retained:
            await record_ingestion_event(
                db,
                practice_id=practice_id,
                actor_user_id=actor_user_id,
                action=AuditAction.retention_skipped,
                resource_type="note",
                resource_id=note.id,
                request_meta=request_meta,
            )

    return NoteGenerationResult(
        note=note,
        degraded=False,
        is_retained=is_retained,
        subjective=soap.subjective,
        objective=soap.objective,
        assessment=soap.assessment,
        plan=soap.plan,
        full_text=soap.full_text,
        provider=soap.provider,
        model=soap.model,
    )


async def _persist_degraded(
    *,
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    transcript_text: str,
    provider: str,
    retry_count: int,
    reason: str,
    request_meta: RequestMeta | None = None,
) -> NoteGenerationResult:
    """DEGRADATION POLICY: when generation fails even after the
    generator's own internal retries, the visit is never lost. A Note
    stub is persisted — status=draft, degraded=True, no SOAP sections
    (none were validated) — with full_text set to the RAW TRANSCRIPT
    text, so the clinician always has something usable to work from and
    the HTTP response's `note.full_text` naturally carries the transcript
    (see app/api/notes.py's GenerateNoteResponse). The session moves to
    SessionStatus.complete_degraded, distinct from `complete` so a
    session list can tell "note ready" apart from "needs manual note
    writing" without joining notes.

    RETENTION: still gated the same as a successful note — if consent
    doesn't allow retention, the persisted stub's full_text is empty too
    (is_retained=False), even though this response always carries the
    real transcript text back to the caller who's entitled to it.
    """
    settings = get_settings()
    async with tenant_session(practice_id) as db:
        is_retained = await ConsentService.assert_retention_allowed(db, session_id=session_id)

        note = Note(
            practice_id=practice_id,
            session_id=session_id,
            format="soap",
            subjective=None,
            objective=None,
            assessment=None,
            plan=None,
            full_text=transcript_text if is_retained else None,
            status=NoteStatus.draft,
            version=1,
            prompt_version=settings.NOTE_PROMPT_VERSION,
            provider=provider,
            model=None,
            degraded=True,
            is_retained=is_retained,
        )
        db.add(note)
        await db.flush()

        encounter = await db.get(EncounterSession, session_id)
        assert encounter is not None
        encounter.status = SessionStatus.complete_degraded
        await db.flush()

        await record_ingestion_event(
            db,
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            action=AuditAction.note_degraded,
            resource_type="note",
            resource_id=note.id,
            metadata={
                "provider": provider,
                "retry_count": retry_count,
                "reason": reason,
                "degraded": True,
                "is_retained": is_retained,
            },
            request_meta=request_meta,
        )
        if not is_retained:
            await record_ingestion_event(
                db,
                practice_id=practice_id,
                actor_user_id=actor_user_id,
                action=AuditAction.retention_skipped,
                resource_type="note",
                resource_id=note.id,
                request_meta=request_meta,
            )

    return NoteGenerationResult(
        note=note,
        degraded=True,
        is_retained=is_retained,
        subjective=None,
        objective=None,
        assessment=None,
        plan=None,
        full_text=transcript_text,
        provider=provider,
        model=None,
    )
