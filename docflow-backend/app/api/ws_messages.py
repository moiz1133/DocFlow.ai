"""Typed message contract for `/v1/sessions/{id}/stream` (Phase 9).

OpenAPI cannot describe a WebSocket protocol, so this module is the
SINGLE SOURCE OF TRUTH the WS handler (app/api/sessions.py) actually
sends/validates through at runtime AND that `scripts/export_ws_schemas.py`
derives `contracts/ws-schemas/*.schema.json` from — the committed JSON
Schemas are generated FROM these models, never hand-maintained
separately, so the contract snapshot test
(tests/test_ws_contract_snapshot.py) genuinely catches wire-format drift
rather than two independently-maintained descriptions silently diverging.

See contracts/ws-protocol.v1.md for the full narrative protocol
description (handshake, ordering, close codes, backpressure).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TranscriptSegmentOut(BaseModel):
    """One diarized segment — shared by the WS transcript.final event and
    the REST finalize response (app/api/sessions.py's TranscriptOut), so
    a client parses the same shape from either path.
    """

    speaker: str | None = Field(default=None, examples=[None])
    start: float | None = Field(default=None, examples=[0.0])
    end: float | None = Field(default=None, examples=[3.4])
    text: str = Field(examples=["Patient reports mild headache since yesterday."])


# --- Client -> server ------------------------------------------------------


class WSAudioFormat(BaseModel):
    mime: str = Field(examples=["audio/webm;codecs=opus"])
    sample_rate: int | None = Field(default=None, examples=[48000])


class WSStartMessage(BaseModel):
    """The mandatory first message on a stream — sent as a JSON text
    frame before any binary audio frame. See contracts/ws-protocol.v1.md
    for what happens if this isn't the first message received.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "start",
                    "format": {"mime": "audio/webm;codecs=opus", "sample_rate": 48000},
                }
            ]
        }
    )

    type: Literal["start"]
    format: WSAudioFormat


class WSStopMessage(BaseModel):
    """Client signals end-of-audio. The server also accepts a plain
    client disconnect as an implicit stop (see the protocol doc) — this
    message is the clean-shutdown path, not the only one.
    """

    model_config = ConfigDict(json_schema_extra={"examples": [{"type": "stop"}]})

    type: Literal["stop"]


# --- Server -> client --------------------------------------------------------


class WSSessionStatusEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"type": "session.status", "status": "recording"}]}
    )

    type: Literal["session.status"] = "session.status"
    # Plain str, not the SessionStatus enum: this module must stay import-
    # free of app.models (kept a standalone leaf so scripts/export_ws_
    # schemas.py never needs a DB/app-settings-configured import chain to
    # render schemas) — app/api/sessions.py passes `.value` in.
    status: str = Field(examples=["created", "recording", "transcribing", "complete"])


class WSTranscriptPartialEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"type": "transcript.partial", "text": "Patient reports mild"}]
        }
    )

    type: Literal["transcript.partial"] = "transcript.partial"
    text: str = Field(examples=["Patient reports mild"])


class WSTranscriptFinalEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "transcript.final",
                    "text": "Patient reports mild headache since yesterday.",
                    "segments": [
                        {
                            "speaker": None,
                            "start": 0.0,
                            "end": 3.4,
                            "text": "Patient reports mild headache since yesterday.",
                        }
                    ],
                }
            ]
        }
    )

    type: Literal["transcript.final"] = "transcript.final"
    text: str = Field(examples=["Patient reports mild headache since yesterday."])
    segments: list[TranscriptSegmentOut]


class WSErrorEvent(BaseModel):
    """Sent before the socket closes on any failure path — `code` is a
    stable machine-readable string (see contracts/ws-protocol.v1.md's
    close-code table for which `code` accompanies which close code).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"type": "error", "code": "unauthorized", "message": "Invalid or expired token"}
            ]
        }
    )

    type: Literal["error"] = "error"
    code: str = Field(
        examples=[
            "unauthorized",
            "session_not_found",
            "protocol_error",
            "rate_limited",
            "audio_bytes_exceeded",
            "session_duration_exceeded",
            "provider_auth_error",
            "provider_rate_limited",
            "provider_timeout",
            "transcription_failed",
        ]
    )
    message: str = Field(examples=["Invalid or expired token"])


__all__ = [
    "TranscriptSegmentOut",
    "WSAudioFormat",
    "WSErrorEvent",
    "WSSessionStatusEvent",
    "WSStartMessage",
    "WSStopMessage",
    "WSTranscriptFinalEvent",
    "WSTranscriptPartialEvent",
]
