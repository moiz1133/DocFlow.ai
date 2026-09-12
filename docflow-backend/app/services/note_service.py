"""Transcript -> scrub -> SOAP note -> persist pipeline (Phase 6).

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
    note: Note
    degraded: bool


async def generate_note_for_session(
    *, session_id: uuid.UUID, practice_id: uuid.UUID, actor_user_id: uuid.UUID
) -> NoteGenerationResult:
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
        )

    return await _persist_success(
        session_id=session_id,
        practice_id=practice_id,
        actor_user_id=actor_user_id,
        soap=soap,
        retry_count=generator.last_retry_count,
    )


async def _persist_success(
    *,
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    soap: SoapNote,
    retry_count: int,
) -> NoteGenerationResult:
    settings = get_settings()
    async with tenant_session(practice_id) as db:
        note = Note(
            practice_id=practice_id,
            session_id=session_id,
            format="soap",
            subjective=soap.subjective,
            objective=soap.objective,
            assessment=soap.assessment,
            plan=soap.plan,
            full_text=soap.full_text,
            status=NoteStatus.draft,
            version=1,
            prompt_version=settings.NOTE_PROMPT_VERSION,
            provider=soap.provider,
            model=soap.model,
            degraded=False,
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
            },
        )
    return NoteGenerationResult(note=note, degraded=False)


async def _persist_degraded(
    *,
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    transcript_text: str,
    provider: str,
    retry_count: int,
    reason: str,
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
    """
    settings = get_settings()
    async with tenant_session(practice_id) as db:
        note = Note(
            practice_id=practice_id,
            session_id=session_id,
            format="soap",
            subjective=None,
            objective=None,
            assessment=None,
            plan=None,
            full_text=transcript_text,
            status=NoteStatus.draft,
            version=1,
            prompt_version=settings.NOTE_PROMPT_VERSION,
            provider=provider,
            model=None,
            degraded=True,
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
            },
        )
    return NoteGenerationResult(note=note, degraded=True)
