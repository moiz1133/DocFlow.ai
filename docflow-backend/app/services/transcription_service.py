"""Shared ingest -> transcribe -> persist pipeline for audio (Phase 5).

Both the WebSocket streaming handler and the POST .../finalize fallback
route (app/api/sessions.py) call into this module so there is exactly one
code path from "audio in" to "Transcript row persisted" — the route
handlers stay transport-only (auth, framing, HTTP/WS status codes) and
delegate everything else here.

ZERO-RETENTION: nothing in this module ever writes raw audio to disk, S3,
or any database column — see the NEVER-PERSIST comments at each point
audio is actually touched. Only text (transcript content/segments) and
metadata (provider name, counts, durations) are ever persisted.

Nothing here imports a vendor SDK; every vendor interaction goes through
the Phase 4 Transcriber interface (app/transcription/base.py).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.session import get_sessionmaker, set_tenant
from app.models import EncounterSession, Transcript
from app.models.enums import AuditAction, SessionStatus
from app.security.consent import ConsentService
from app.services.audit_service import RequestMeta, record_phi_access
from app.transcription.base import (
    AudioChunk,
    TranscriptionAuthError,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionRateLimitError,
    TranscriptionResult,
    TranscriptionTimeoutError,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)


class SessionLimitExceeded(Exception):
    """Raised when a streaming session exceeds MAX_AUDIO_BYTES or
    MAX_SESSION_SECONDS. Carries a machine-readable code and a PHI-free
    message suitable for the client-facing {"type": "error", ...} event.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# DB session helper
# ---------------------------------------------------------------------------


@asynccontextmanager
async def tenant_session(practice_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """A short-lived, tenant-scoped transaction — the WS-loop equivalent of
    app.auth.dependencies.get_tenant_session, which is FastAPI-dependency
    shaped and can't be reused inside a long-lived WS connection. Commits
    on clean exit, rolls back on exception (session.begin()'s own
    behavior); call this once per discrete unit of work rather than
    holding one transaction open for a whole WS connection, so concurrent
    readers (e.g. GET /v1/sessions/{id}) see status transitions as they
    happen.
    """
    async with get_sessionmaker()() as session, session.begin():
        await set_tenant(session, practice_id)
        yield session


async def set_session_status(
    db: AsyncSession, encounter: EncounterSession, status: SessionStatus
) -> None:
    encounter.status = status
    await db.flush()


# ---------------------------------------------------------------------------
# Audio buffering / backpressure
# ---------------------------------------------------------------------------


async def audio_chunk_stream(
    queue: asyncio.Queue[AudioChunk | None],
) -> AsyncIterator[AudioChunk]:
    """Bridges a bounded producer/consumer queue into the AsyncIterable
    shape Transcriber.stream() expects. A `None` item is the end-of-audio
    sentinel.

    NEVER-PERSIST: each chunk is yielded straight through to the
    transcriber and then immediately dropped — this function keeps no
    list, buffer, or copy of anything it has already yielded.
    """
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk


def check_limits(*, total_audio_bytes: int, elapsed_seconds: float, settings: Settings) -> None:
    """Raises SessionLimitExceeded if either hard ceiling has been passed.
    Called on every incoming audio frame so a runaway client is stopped
    promptly rather than only at end-of-stream.
    """
    if total_audio_bytes > settings.MAX_AUDIO_BYTES:
        raise SessionLimitExceeded(
            "audio_bytes_exceeded",
            f"Maximum audio size of {settings.MAX_AUDIO_BYTES} bytes exceeded",
        )
    if elapsed_seconds > settings.MAX_SESSION_SECONDS:
        raise SessionLimitExceeded(
            "session_duration_exceeded",
            f"Maximum session duration of {settings.MAX_SESSION_SECONDS} seconds exceeded",
        )


# ---------------------------------------------------------------------------
# Streaming aggregation — turns a series of TranscriptionEvents into one
# persistable TranscriptionResult-shaped aggregate, with no second vendor
# call and no re-buffered audio.
# ---------------------------------------------------------------------------


@dataclass
class StreamAggregate:
    """Accumulates a Transcriber.stream() run's "final" events into one
    persistable result. Holds only text/segments — never audio.

    Works for both current implementations despite their different final-
    event semantics: MockTranscriber yields exactly one final event
    carrying the complete text; OpenAITranscriber yields one final event
    per fixed-size window carrying just that window's text (see
    app/transcription/openai.py's stream() docstring) — concatenating
    events in arrival order reconstructs the full transcript either way.
    """

    _text_parts: list[str] = field(default_factory=list)
    _segments: list[TranscriptSegment] = field(default_factory=list)

    def add_final_event(self, event: TranscriptionEvent) -> None:
        if not event.text:
            return
        self._text_parts.append(event.text)
        # Mirrors app/transcription/openai.py's own "no segments in the
        # response -> treat the whole window as one segment" fallback.
        self._segments.append(event.segment or TranscriptSegment(text=event.text))

    @property
    def has_content(self) -> bool:
        return bool(self._text_parts)

    def to_result(self, *, provider: str) -> TranscriptionResult:
        return TranscriptionResult(
            segments=list(self._segments),
            full_text=" ".join(self._text_parts),
            provider=provider,
        )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


async def record_ingestion_event(
    db: AsyncSession,
    *,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: AuditAction,
    resource_type: str,
    resource_id: uuid.UUID | None,
    metadata: dict[str, object] | None = None,
    request_meta: RequestMeta | None = None,
) -> None:
    """PHI-free audit entry for a session/transcript lifecycle event. Never
    pass transcript text or audio here — see app/models/audit_log.py.

    A thin, historically-named wrapper around
    app/services/audit_service.py's record_phi_access (the Phase 7 choke
    point covering reads too, not just writes) — kept so the existing
    call sites in this module and app/api/sessions.py don't need to
    change.
    """
    await record_phi_access(
        db,
        practice_id=practice_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata,
        request_meta=request_meta,
    )


# ---------------------------------------------------------------------------
# Persistence — the one code path shared by the WS handler and the
# finalize route.
# ---------------------------------------------------------------------------


async def persist_transcript(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    full_text: str,
    segments: list[TranscriptSegment],
    provider: str,
) -> Transcript:
    """Creates or replaces the Transcript row for a session, applying the
    retention policy, and writes the transcript.created audit entry.

    Retention gating: when is_retained resolves False, this deliberately
    stores an EMPTY content/segments row rather than the real transcript
    — a Transcript row still exists (so "does a transcript exist for this
    session" stays meaningful and re-finalizing stays idempotent), but no
    PHI reaches the database. Callers always have the real full_text/
    segments in hand already (from transcribe() or a StreamAggregate) and
    should build their client-facing response from THOSE, not from the
    row this returns — see app/api/sessions.py's finalize route.
    """
    is_retained = await ConsentService.assert_retention_allowed(db, session_id=session_id)

    stored_content = full_text if is_retained else ""
    stored_segments = [segment.to_jsonb() for segment in segments] if is_retained else []

    existing = await db.execute(select(Transcript).where(Transcript.session_id == session_id))
    transcript = existing.scalar_one_or_none()
    if transcript is None:
        transcript = Transcript(
            practice_id=practice_id,
            session_id=session_id,
            content=stored_content,
            segments=stored_segments,
            is_retained=is_retained,
        )
        db.add(transcript)
    else:
        transcript.content = stored_content
        transcript.segments = stored_segments
        transcript.is_retained = is_retained
    await db.flush()

    await record_ingestion_event(
        db,
        practice_id=practice_id,
        actor_user_id=actor_user_id,
        action=AuditAction.create,
        resource_type="transcript",
        resource_id=transcript.id,
        metadata={
            "provider": provider,
            "is_retained": is_retained,
            "segment_count": len(segments),
        },
    )
    if not is_retained:
        # A distinct decision audit alongside transcript.created (above)
        # — see app/security/consent.py's module docstring.
        await record_ingestion_event(
            db,
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            action=AuditAction.retention_skipped,
            resource_type="transcript",
            resource_id=transcript.id,
        )
    return transcript


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

_ERROR_CODE_BY_EXCEPTION: dict[type[TranscriptionError], str] = {
    TranscriptionAuthError: "provider_auth_error",
    TranscriptionRateLimitError: "provider_rate_limited",
    TranscriptionTimeoutError: "provider_timeout",
}


def error_code_for(exc: TranscriptionError) -> str:
    """Maps a typed Transcriber exception to a machine-readable, PHI-free
    error code for the client. Never surfaces the vendor exception's own
    message (which could, in principle, echo request content).
    """
    return _ERROR_CODE_BY_EXCEPTION.get(type(exc), "transcription_failed")


def http_status_for(exc: TranscriptionError) -> int:
    """HTTP status for the POST .../finalize fallback (app/api/sessions.py).
    Every case is a failure of *our* upstream call to the vendor, not of
    the caller's request, so nothing here is a 4xx.
    """
    if isinstance(exc, TranscriptionRateLimitError):
        return 503  # Service Unavailable
    if isinstance(exc, TranscriptionTimeoutError):
        return 504  # Gateway Timeout
    return 502  # Bad Gateway — auth errors and anything else untranslated
