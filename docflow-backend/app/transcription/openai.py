"""OpenAITranscriber — the ONLY module in this codebase allowed to import
the `openai` SDK. See app/transcription/base.py's module docstring for
why that boundary matters, tests/test_transcription_isolation.py for the
test (and pyproject.toml's `flake8-tidy-imports` banned-api rule for the
lint-level enforcement) that keeps it true.

This is never exercised against the real network in CI — see
tests/test_transcription_openai.py, which mocks the SDK client entirely.
Real transcription of real (non-synthetic) audio additionally requires a
signed BAA *and* a Zero Data Retention agreement with OpenAI; running
vendor="openai" without both is a compliance violation, not just a
config mistake — see app/transcription/factory.py's guardrails.
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterable, AsyncIterator

import openai

from app.config import Settings
from app.transcription.base import (
    AudioChunk,
    AudioSource,
    Transcriber,
    TranscriptionAuthError,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionRateLimitError,
    TranscriptionResult,
    TranscriptionTimeoutError,
    TranscriptSegment,
    materialize_chunks,
)

logger = logging.getLogger(__name__)

# OpenAI's file-based transcription endpoint caps upload size well below
# this; staying meaningfully under 25MB leaves headroom for multipart
# framing overhead without needing to compute it precisely.
_MAX_WINDOW_BYTES = 24 * 1024 * 1024

# Chunks-per-window for the *streaming emulation* (stream()), independent
# of the byte-size windowing transcribe() uses for the vendor's file-size
# limit. In a live pipeline (Phase 5) each AudioChunk is expected to be a
# small, short slice (roughly 100ms-1s of audio), so this default buffers
# roughly a few seconds before emitting a "final" event per window.
_DEFAULT_STREAM_WINDOW_CHUNK_COUNT = 20

_EXTENSION_BY_MIME = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}


def _extension_for_mime(mime_type: str) -> str:
    return _EXTENSION_BY_MIME.get(mime_type.lower(), "bin")


def _translate_error(exc: Exception) -> TranscriptionError:
    """Maps an openai SDK exception (or our own timeout) to one of the
    vendor-neutral exceptions callers are allowed to catch. Never called
    with, and never returns, a raw openai.* exception.
    """
    if isinstance(exc, TimeoutError):
        return TranscriptionTimeoutError(str(exc) or "OpenAI transcription request timed out")
    if isinstance(exc, openai.APITimeoutError):
        return TranscriptionTimeoutError(str(exc))
    if isinstance(exc, openai.AuthenticationError):
        return TranscriptionAuthError(str(exc))
    if isinstance(exc, openai.RateLimitError):
        return TranscriptionRateLimitError(str(exc))
    if isinstance(exc, openai.OpenAIError):
        return TranscriptionError(str(exc))
    return TranscriptionError(f"unexpected OpenAI transcription failure: {exc}")


def _retry_delay_seconds(attempt: int) -> float:
    """Exponential backoff with jitter: 0.5s, 1s, 2s, ... plus up to 250ms
    of random jitter to avoid a thundering herd on shared rate limits.
    """
    base = 0.5 * (2.0**attempt)
    return base + random.uniform(0, 0.25)


class OpenAITranscriber(Transcriber):
    provider_name = "openai"

    def __init__(self, settings: Settings, *, stream_window_chunk_count: int | None = None) -> None:
        if not settings.OPENAI_API_KEY:
            # Defensive: the factory is the intended gate for this, but a
            # direct construction (e.g. in a test) should fail the same way.
            raise TranscriptionAuthError("OPENAI_API_KEY is not configured")
        self._client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self._model = settings.OPENAI_TRANSCRIBE_MODEL
        self._timeout_seconds = settings.TRANSCRIBE_TIMEOUT_SECONDS
        self._max_retries = settings.TRANSCRIBE_MAX_RETRIES
        self._stream_window_chunk_count = (
            stream_window_chunk_count or _DEFAULT_STREAM_WINDOW_CHUNK_COUNT
        )

    async def transcribe(
        self,
        chunks: AudioSource,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        resolved = await materialize_chunks(chunks)
        if not resolved:
            return TranscriptionResult(
                segments=[], full_text="", provider=self.provider_name, language=language
            )

        windows = self._split_into_windows(resolved)
        window_results = [
            await self._transcribe_window(window, language=language) for window in windows
        ]
        return _stitch(window_results, provider=self.provider_name)

    async def stream(self, chunks: AsyncIterable[AudioChunk]) -> AsyncIterator[TranscriptionEvent]:
        """Emulated streaming.

        OpenAI's file-based transcription API has no true real-time
        streaming for this use case: we buffer incoming chunks into
        fixed-size windows and call transcribe() once per window, then
        yield a single "final" event per window (never "partial" — we
        have no visibility into a window's transcript until it's
        complete). A genuinely streaming vendor (e.g. Deepgram) would
        override this method entirely with a real partial-event cadence
        instead of emulating one.
        """
        window: list[AudioChunk] = []
        async for chunk in chunks:
            window.append(chunk)
            if len(window) >= self._stream_window_chunk_count:
                event = await self._window_to_event(window)
                if event is not None:
                    yield event
                window = []

        if window:
            event = await self._window_to_event(window)
            if event is not None:
                yield event

    async def _window_to_event(self, window: list[AudioChunk]) -> TranscriptionEvent | None:
        result = await self.transcribe(window)
        if not result.full_text:
            return None
        merged_segment = _merge_segments(result.segments, fallback_text=result.full_text)
        return TranscriptionEvent(type="final", text=result.full_text, segment=merged_segment)

    def _split_into_windows(self, chunks: list[AudioChunk]) -> list[list[AudioChunk]]:
        """Groups chunks so each window's total byte size stays under the
        vendor's upload limit — see _MAX_WINDOW_BYTES.
        """
        windows: list[list[AudioChunk]] = []
        current: list[AudioChunk] = []
        current_bytes = 0

        for chunk in chunks:
            chunk_size = len(chunk.data)
            if current and current_bytes + chunk_size > _MAX_WINDOW_BYTES:
                windows.append(current)
                current = []
                current_bytes = 0
            current.append(chunk)
            current_bytes += chunk_size

        if current:
            windows.append(current)
        return windows

    async def _transcribe_window(
        self, window: list[AudioChunk], *, language: str | None
    ) -> TranscriptionResult:
        audio_bytes = b"".join(chunk.data for chunk in window)
        mime_type = window[0].mime_type
        file_tuple = (f"audio.{_extension_for_mime(mime_type)}", audio_bytes, mime_type)

        last_error: TranscriptionError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                # mypy can't cleanly resolve this SDK call's overload set
                # (literal-dispatched response_format crossed with a
                # dynamically-built file tuple) — runtime behavior is
                # covered by tests/test_transcription_openai.py's mocked
                # client instead.
                response = await asyncio.wait_for(
                    self._client.audio.transcriptions.create(  # type: ignore[call-overload]
                        model=self._model,
                        file=file_tuple,
                        language=language or openai.NOT_GIVEN,
                        response_format="verbose_json",
                    ),
                    timeout=self._timeout_seconds,
                )
                return _parse_response(
                    response, provider=self.provider_name, requested_language=language
                )
            except Exception as exc:  # translated immediately below, never re-raised as-is
                translated = _translate_error(exc)
                last_error = translated
                is_retryable = isinstance(
                    translated, TranscriptionRateLimitError | TranscriptionTimeoutError
                )
                if not is_retryable or attempt == self._max_retries:
                    raise translated from exc
                logger.warning(
                    "openai transcription attempt failed, retrying",
                    extra={"attempt": attempt, "error_type": type(translated).__name__},
                )
                await asyncio.sleep(_retry_delay_seconds(attempt))

        # Unreachable in practice (the loop above always raises or
        # returns), but keeps mypy happy about the function's return type.
        assert last_error is not None
        raise last_error


def _parse_response(
    response: object, *, provider: str, requested_language: str | None
) -> TranscriptionResult:
    text = (getattr(response, "text", "") or "").strip()
    language = getattr(response, "language", None) or requested_language
    duration = getattr(response, "duration", None)
    raw_segments = getattr(response, "segments", None)

    segments: list[TranscriptSegment]
    if raw_segments:
        segments = [
            TranscriptSegment(
                text=(getattr(seg, "text", "") or "").strip(),
                speaker=None,  # OpenAI does not diarize — see module docstring.
                start=getattr(seg, "start", None),
                end=getattr(seg, "end", None),
            )
            for seg in raw_segments
        ]
    elif text:
        # response_format didn't return segment-level detail (model-
        # dependent); treat the whole window as one segment.
        segments = [TranscriptSegment(text=text, speaker=None, start=None, end=duration)]
    else:
        segments = []

    return TranscriptionResult(
        segments=segments,
        full_text=text,
        provider=provider,
        language=language,
        duration_seconds=float(duration) if duration is not None else None,
    )


def _stitch(results: list[TranscriptionResult], *, provider: str) -> TranscriptionResult:
    """Combines per-window results (from transcribe()'s file-size
    windowing) into one TranscriptionResult, in window order.
    """
    segments = [segment for result in results for segment in result.segments]
    full_text = " ".join(result.full_text for result in results if result.full_text)
    total_duration = sum(result.duration_seconds or 0.0 for result in results)
    language = next((result.language for result in results if result.language), None)

    return TranscriptionResult(
        segments=segments,
        full_text=full_text,
        provider=provider,
        language=language,
        duration_seconds=total_duration or None,
    )


def _merge_segments(
    segments: list[TranscriptSegment], *, fallback_text: str
) -> TranscriptSegment | None:
    """Collapses a window's segments into one, for a single streaming
    "final" event. Timing is deliberately not synthesized across a merge
    (start/end become the outer bounds only when every segment actually
    has them) — an approximate span is worse than no span.
    """
    if not segments:
        return None
    starts = [s.start for s in segments if s.start is not None]
    ends = [s.end for s in segments if s.end is not None]
    return TranscriptSegment(
        text=fallback_text,
        speaker=None,
        start=min(starts) if len(starts) == len(segments) else None,
        end=max(ends) if len(ends) == len(segments) else None,
    )
