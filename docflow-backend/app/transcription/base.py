"""Vendor-neutral transcription interface.

Nothing outside app/transcription/ may import a vendor SDK. Callers
(Phase 5's audio ingest, Phase 6's note generation) depend only on the
types and abstract class defined here — see app/transcription/factory.py
for how a concrete implementation gets selected, and
tests/test_transcription_isolation.py for the guardrail that keeps this
true.

Every vendor implementation, mock included, produces `TranscriptSegment`
objects shaped exactly like the Phase 2 `Transcript.segments` JSONB
column ({speaker, start, end, text}) so persistence never needs to know
which vendor produced a result.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Vendor-neutral data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """A normalized unit of audio input. Callers never pass vendor-specific
    payloads (a raw OpenAI file object, a Deepgram websocket frame, ...) —
    everything funnels through this shape first.
    """

    data: bytes
    mime_type: str  # e.g. "audio/wav", "audio/webm", "audio/mpeg"
    sample_rate: int | None = None
    # Ordering hint for chunks arriving out of order (unused by AudioChunk
    # itself; a caller streaming from an unordered source can set it).
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One span of transcript text. Mirrors Transcript.segments' JSONB
    shape exactly: {speaker, start, end, text}.

    speaker is None for any non-diarizing vendor (OpenAI, v1) — the field
    exists from day one so a diarizing vendor (Phase 5+/later) can start
    populating it with zero shape changes downstream.
    """

    text: str
    speaker: str | None = None
    start: float | None = None
    end: float | None = None

    def to_jsonb(self) -> dict[str, object]:
        """The exact dict shape Transcript.segments (JSONB) expects."""
        return {"speaker": self.speaker, "start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """The outcome of a full (batch) transcription call."""

    segments: list[TranscriptSegment]
    full_text: str
    provider: str
    language: str | None = None
    duration_seconds: float | None = None


TranscriptionEventType = Literal["partial", "final"]


@dataclass(frozen=True, slots=True)
class TranscriptionEvent:
    """One item from Transcriber.stream().

    `text` is always populated (the partial or final text so far).
    `segment` is populated only for "final" events where the vendor (or
    emulation layer) can attach segment-level metadata; partials never
    carry one, since their timing/boundaries aren't settled yet.
    """

    type: TranscriptionEventType
    text: str
    segment: TranscriptSegment | None = None


# A caller may hand us either a pre-collected list (a whole recorded
# session already in memory) or an async source (audio arriving live).
# Both `transcribe()` overloads normalize through `materialize_chunks`.
AudioSource = AsyncIterable[AudioChunk] | list[AudioChunk]


async def materialize_chunks(chunks: AudioSource) -> list[AudioChunk]:
    """Drains an AudioSource into a plain list, once.

    Shared by every Transcriber implementation so each one doesn't
    reinvent "is this a list or an async iterable" handling.
    """
    if isinstance(chunks, list):
        return chunks
    return [chunk async for chunk in chunks]


# ---------------------------------------------------------------------------
# Typed exceptions — vendor SDK errors must never escape an implementation
# ---------------------------------------------------------------------------


class TranscriptionError(Exception):
    """Base for every transcription failure. Callers should catch this (or
    a subclass) and never a vendor SDK exception directly — implementations
    are required to translate.
    """


class TranscriptionAuthError(TranscriptionError):
    """The vendor rejected our credentials."""


class TranscriptionRateLimitError(TranscriptionError):
    """The vendor rate-limited this request."""


class TranscriptionTimeoutError(TranscriptionError):
    """The vendor call exceeded the configured timeout."""


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class Transcriber(ABC):
    """Vendor-neutral transcription interface.

    `provider_name` is a plain string (e.g. "mock", "openai") logged at
    startup and stamped onto every TranscriptionResult — never content,
    never anything vendor-SDK-shaped.
    """

    provider_name: str

    @abstractmethod
    async def transcribe(
        self,
        chunks: AudioSource,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Batch/whole-utterance transcription — the primary path for a
        vendor (like OpenAI) with no true real-time streaming API.
        """
        raise NotImplementedError

    @abstractmethod
    def stream(self, chunks: AsyncIterable[AudioChunk]) -> AsyncIterator[TranscriptionEvent]:
        """Yields partial/final transcription events as audio arrives.

        A genuinely streaming vendor overrides this with a real
        partial-event cadence. A non-streaming vendor emulates it — see
        OpenAITranscriber.stream's docstring for that emulation's shape.
        """
        raise NotImplementedError


__all__ = [
    "AudioChunk",
    "AudioSource",
    "Transcriber",
    "TranscriptSegment",
    "TranscriptionAuthError",
    "TranscriptionError",
    "TranscriptionEvent",
    "TranscriptionEventType",
    "TranscriptionRateLimitError",
    "TranscriptionResult",
    "TranscriptionTimeoutError",
    "materialize_chunks",
]
