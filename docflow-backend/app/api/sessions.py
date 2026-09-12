"""Session lifecycle, audio ingestion, and transcript finalization.

Phase 5: this module ends at a stored transcript. SOAP note generation
(Phase 6) is deliberately out of scope — SessionStatus.generating exists
on the model but nothing here ever sets it.

=== WebSocket auth ===
A browser or extension WebSocket handshake cannot reliably set a custom
Authorization header (the browser APIs that initiate a WS connection
don't expose one), so the access token is instead passed as a query
parameter: `wss://.../v1/sessions/{id}/stream?token=<access_token>`.
This is validated identically to get_current_user (decode + active-user +
tenant match) before the socket does anything else. The tradeoff — query
strings can end up in server access logs — is accepted deliberately here:
access tokens are already short-lived (15 minutes by default) and this
endpoint must be served over WSS in any non-dev environment, same as
every other endpoint in this API.

=== Zero-retention ===
Raw audio is never written to disk, S3, or any database column, anywhere
in this module or app/services/transcription_service.py, which this
module delegates all ingest/transcribe/persist logic to. Every point
audio is actually held (the WS per-connection queue, the finalize
route's in-memory upload) is marked with a NEVER-PERSIST comment.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.websockets import WebSocket, WebSocketState
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_tenant_session
from app.auth.tokens import AccessTokenClaims, TokenError, decode_access_token
from app.config import get_settings
from app.models import EncounterSession, Transcript, User
from app.models.enums import AuditAction, SessionStatus
from app.services.transcription_service import (
    SessionLimitExceeded,
    StreamAggregate,
    audio_chunk_stream,
    check_limits,
    error_code_for,
    http_status_for,
    persist_transcript,
    record_ingestion_event,
    set_session_status,
    tenant_session,
)
from app.transcription.base import (
    AudioChunk,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptSegment,
)
from app.transcription.factory import get_transcriber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

# Application-range WS close codes (4000-4999 is reserved for this by the
# WS spec). 1000/1002/1011 below are standard codes (normal closure,
# protocol error, internal error) used where no bespoke code is needed.
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_NOT_FOUND = 4404
_CLOSE_LIMIT_EXCEEDED = 4413


# --- Schemas ---------------------------------------------------------------


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID


class SessionStatusResponse(BaseModel):
    session_id: uuid.UUID
    status: SessionStatus
    transcript_exists: bool


class TranscriptSegmentOut(BaseModel):
    speaker: str | None
    start: float | None
    end: float | None
    text: str


class TranscriptOut(BaseModel):
    content: str
    segments: list[TranscriptSegmentOut]
    is_retained: bool


class FinalizeResponse(BaseModel):
    transcript: TranscriptOut


class _AudioFormat(BaseModel):
    mime: str
    sample_rate: int | None = None


class _StartMessage(BaseModel):
    type: Literal["start"]
    format: _AudioFormat


# --- REST routes -------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> CreateSessionResponse:
    encounter = EncounterSession(practice_id=user.practice_id, clinician_id=user.id)
    db.add(encounter)
    await db.flush()
    await record_ingestion_event(
        db,
        practice_id=user.practice_id,
        actor_user_id=user.id,
        action=AuditAction.create,
        resource_type="session",
        resource_id=encounter.id,
    )
    return CreateSessionResponse(session_id=encounter.id)


@router.get("/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> SessionStatusResponse:
    # No manual tenant check: RLS (scoped by get_tenant_session) makes a
    # session belonging to another practice invisible, same as
    # app/api/users.py's get_user — a guessed id from another tenant comes
    # back 404, not 403, so its existence is never confirmed either way.
    encounter = await db.get(EncounterSession, session_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    transcript_row = await db.execute(
        select(Transcript.id).where(Transcript.session_id == session_id)
    )
    return SessionStatusResponse(
        session_id=encounter.id,
        status=encounter.status,
        transcript_exists=transcript_row.first() is not None,
    )


@router.post("/{session_id}/finalize")
async def finalize_session(
    session_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_tenant_session)],
    audio: Annotated[UploadFile, File(description="A whole recorded session's audio")],
) -> FinalizeResponse:
    """Non-streaming fallback: accept a complete audio upload, transcribe
    it in one batch call, and persist identically to the WS path (both
    call app.services.transcription_service.persist_transcript).
    """
    settings = get_settings()
    encounter = await db.get(EncounterSession, session_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    # NEVER-PERSIST: `audio_bytes` is handed to the transcriber below and
    # never written to disk, S3, or any DB column. `audio` itself (a
    # SpooledTemporaryFile FastAPI discards once this request ends) is
    # never copied anywhere durable either.
    audio_bytes = await audio.read()
    if len(audio_bytes) > settings.MAX_AUDIO_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Audio exceeds maximum allowed size"
        )
    if not audio_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty audio upload")

    chunk = AudioChunk(data=audio_bytes, mime_type=audio.content_type or "application/octet-stream")
    transcriber = get_transcriber()

    try:
        result = await transcriber.transcribe([chunk])
    except TranscriptionError as exc:
        await set_session_status(db, encounter, SessionStatus.error)
        await record_ingestion_event(
            db,
            practice_id=user.practice_id,
            actor_user_id=user.id,
            action=AuditAction.update,
            resource_type="session",
            resource_id=session_id,
            metadata={"status": "error", "reason": error_code_for(exc)},
        )
        raise HTTPException(http_status_for(exc), "Transcription failed") from exc

    await set_session_status(db, encounter, SessionStatus.transcribing)
    transcript = await persist_transcript(
        db,
        session_id=session_id,
        practice_id=user.practice_id,
        actor_user_id=user.id,
        full_text=result.full_text,
        segments=result.segments,
        provider=result.provider,
    )
    await set_session_status(db, encounter, SessionStatus.complete)

    # The response always carries the real, in-memory result regardless of
    # is_retained — retention only governs what got written to `transcript`
    # (the DB row), never what the caller who just submitted this audio is
    # told back.
    return FinalizeResponse(
        transcript=TranscriptOut(
            content=result.full_text,
            segments=[TranscriptSegmentOut(**segment.to_jsonb()) for segment in result.segments],
            is_retained=transcript.is_retained,
        )
    )


# --- WebSocket streaming -----------------------------------------------------


@dataclass
class _ReceiveState:
    total_bytes: int = 0
    any_audio_received: bool = False
    ended_via: Literal["stop", "disconnect"] | None = None


def _parse_json_object(text: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _send_json_safe(websocket: WebSocket, payload: dict[str, object]) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.send_json(payload)
    except RuntimeError:
        # The client went away between our state check and the send.
        logger.debug("WS send skipped: connection already closing")


async def _send_status(websocket: WebSocket, session_status: SessionStatus) -> None:
    await _send_json_safe(websocket, {"type": "session.status", "status": session_status.value})


async def _send_error(websocket: WebSocket, *, code: str, message: str) -> None:
    await _send_json_safe(websocket, {"type": "error", "code": code, "message": message})


async def _send_transcript_event(websocket: WebSocket, event: TranscriptionEvent) -> None:
    if event.type == "partial":
        await _send_json_safe(websocket, {"type": "transcript.partial", "text": event.text})
        return
    # Mirrors StreamAggregate.add_final_event's own fallback (see
    # app/services/transcription_service.py) so what the client sees
    # matches what ends up persisted: a final event with no vendor-
    # supplied segment (MockTranscriber never sets one) still gets a
    # synthesized one here rather than reporting empty segments for
    # non-empty text.
    segments: list[dict[str, object]] = []
    if event.text:
        segment = event.segment or TranscriptSegment(text=event.text)
        segments = [segment.to_jsonb()]
    await _send_json_safe(
        websocket, {"type": "transcript.final", "text": event.text, "segments": segments}
    )


async def _close_ws(websocket: WebSocket, *, code: int) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code)
    except RuntimeError:
        logger.debug("WS close skipped: connection already closed")


async def _authenticate_ws(websocket: WebSocket) -> AccessTokenClaims | None:
    """Validates the `token` query param the same way get_current_user
    validates a bearer header: decode -> active user -> tenant match.
    Sends {"type": "error"} and closes 4401 on any failure.
    """
    token = websocket.query_params.get("token")
    if not token:
        await _send_error(websocket, code="unauthorized", message="Missing access token")
        await _close_ws(websocket, code=_CLOSE_UNAUTHORIZED)
        return None

    try:
        claims = decode_access_token(token)
    except TokenError:
        await _send_error(websocket, code="unauthorized", message="Invalid or expired token")
        await _close_ws(websocket, code=_CLOSE_UNAUTHORIZED)
        return None

    async with tenant_session(claims.practice_id) as db:
        user = await db.get(User, claims.user_id)

    if user is None or not user.is_active or user.practice_id != claims.practice_id:
        await _send_error(websocket, code="unauthorized", message="Invalid or expired token")
        await _close_ws(websocket, code=_CLOSE_UNAUTHORIZED)
        return None

    return claims


async def _await_start_message(websocket: WebSocket) -> _StartMessage | None:
    """Blocks for the client's first message, which must be a JSON
    {"type": "start", "format": {...}} control frame. Sends an error and
    closes on anything else (disconnect, binary-before-start, malformed
    JSON, wrong message type).
    """
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None

    text = message.get("text")
    control = _parse_json_object(text) if text is not None else None
    if control is None or control.get("type") != "start":
        await _send_error(
            websocket, code="protocol_error", message='Expected a {"type": "start", ...} message'
        )
        await _close_ws(websocket, code=1002)
        return None

    try:
        return _StartMessage.model_validate(control)
    except ValidationError:
        await _send_error(websocket, code="protocol_error", message="Malformed start message")
        await _close_ws(websocket, code=1002)
        return None


@router.websocket("/{session_id}/stream")
async def stream_session(websocket: WebSocket, session_id: uuid.UUID) -> None:
    settings = get_settings()
    await websocket.accept()

    claims = await _authenticate_ws(websocket)
    if claims is None:
        return

    async with tenant_session(claims.practice_id) as db:
        encounter = await db.get(EncounterSession, session_id)
        session_status = encounter.status if encounter is not None else None

    if encounter is None or session_status is None:
        await _send_error(websocket, code="session_not_found", message="Session not found")
        await _close_ws(websocket, code=_CLOSE_NOT_FOUND)
        return

    await _send_status(websocket, session_status)

    start_message = await _await_start_message(websocket)
    if start_message is None:
        # Either the client vanished or sent something we couldn't use
        # before ever recording anything — nothing to finalize.
        async with tenant_session(claims.practice_id) as db:
            encounter = await db.get(EncounterSession, session_id)
            if encounter is not None:
                await set_session_status(db, encounter, SessionStatus.error)
        return

    async with tenant_session(claims.practice_id) as db:
        encounter = await db.get(EncounterSession, session_id)
        if encounter is None:
            await _close_ws(websocket, code=_CLOSE_NOT_FOUND)
            return
        await set_session_status(db, encounter, SessionStatus.recording)
        await record_ingestion_event(
            db,
            practice_id=claims.practice_id,
            actor_user_id=claims.user_id,
            action=AuditAction.stream_started,
            resource_type="session",
            resource_id=session_id,
        )
    await _send_status(websocket, SessionStatus.recording)

    transcriber = get_transcriber()
    # Bounded queue: a producer (the receive loop, below) that outruns the
    # consumer (the transcriber pump) blocks on queue.put — backpressure,
    # not unbounded growth. See app/services/transcription_service.py.
    queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=settings.WS_BUFFER_MAX_CHUNKS)
    aggregate = StreamAggregate()
    receive_state = _ReceiveState()
    started_at = time.monotonic()

    async def _receive_loop() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    receive_state.ended_via = "disconnect"
                    return

                data = message.get("bytes")
                if data is not None:
                    receive_state.total_bytes += len(data)
                    receive_state.any_audio_received = True
                    check_limits(
                        total_audio_bytes=receive_state.total_bytes,
                        elapsed_seconds=time.monotonic() - started_at,
                        settings=settings,
                    )
                    # NEVER-PERSIST: `data` goes straight onto the bounded
                    # queue (consumed by the transcriber pump below) and
                    # this loop keeps no other reference to it.
                    await queue.put(
                        AudioChunk(
                            data=data,
                            mime_type=start_message.format.mime,
                            sample_rate=start_message.format.sample_rate,
                        )
                    )
                    continue

                text = message.get("text")
                control = _parse_json_object(text) if text is not None else None
                if control is not None and control.get("type") == "stop":
                    receive_state.ended_via = "stop"
                    return
                # Any other/malformed control frame is ignored — lenient
                # to unknown future message types rather than tearing
                # down an otherwise-healthy stream.
        finally:
            # Unblocks the transcriber pump's async-for so it can drain
            # whatever is already queued and return.
            await queue.put(None)

    async def _pump_transcriber() -> None:
        async for event in transcriber.stream(audio_chunk_stream(queue)):
            if event.type == "final":
                aggregate.add_final_event(event)
            await _send_transcript_event(websocket, event)

    receiver_task = asyncio.create_task(_receive_loop())
    pump_task = asyncio.create_task(_pump_transcriber())

    done, pending = await asyncio.wait(
        {receiver_task, pump_task}, return_when=asyncio.FIRST_EXCEPTION
    )
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    limit_error: SessionLimitExceeded | None = None
    transcription_error: TranscriptionError | None = None
    for task in done:
        exc = task.exception()
        if exc is None:
            continue
        if isinstance(exc, SessionLimitExceeded):
            limit_error = exc
        elif isinstance(exc, TranscriptionError):
            transcription_error = exc
        else:
            raise exc

    if limit_error is not None:
        await _send_error(websocket, code=limit_error.code, message=limit_error.message)
        async with tenant_session(claims.practice_id) as db:
            encounter = await db.get(EncounterSession, session_id)
            if encounter is not None:
                await set_session_status(db, encounter, SessionStatus.error)
        await _close_ws(websocket, code=_CLOSE_LIMIT_EXCEEDED)
        return

    if transcription_error is not None:
        code = error_code_for(transcription_error)
        await _send_error(websocket, code=code, message="Transcription failed")
        async with tenant_session(claims.practice_id) as db:
            encounter = await db.get(EncounterSession, session_id)
            if encounter is not None:
                await set_session_status(db, encounter, SessionStatus.error)
                await record_ingestion_event(
                    db,
                    practice_id=claims.practice_id,
                    actor_user_id=claims.user_id,
                    action=AuditAction.update,
                    resource_type="session",
                    resource_id=session_id,
                    metadata={"status": "error", "reason": code},
                )
        await _close_ws(websocket, code=1011)
        return

    async with tenant_session(claims.practice_id) as db:
        encounter = await db.get(EncounterSession, session_id)
        if encounter is None:
            await _close_ws(websocket, code=_CLOSE_NOT_FOUND)
            return

        if receive_state.ended_via == "disconnect" and not receive_state.any_audio_received:
            await set_session_status(db, encounter, SessionStatus.error)
            await _close_ws(websocket, code=1000)
            return

        await set_session_status(db, encounter, SessionStatus.transcribing)
        result = aggregate.to_result(provider=transcriber.provider_name)
        await persist_transcript(
            db,
            session_id=session_id,
            practice_id=claims.practice_id,
            actor_user_id=claims.user_id,
            full_text=result.full_text,
            segments=result.segments,
            provider=result.provider,
        )
        await set_session_status(db, encounter, SessionStatus.complete)

    await _send_status(websocket, SessionStatus.complete)
    await _close_ws(websocket, code=1000)
