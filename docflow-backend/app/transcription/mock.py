"""MockTranscriber: no network, no keys, deterministic output.

The default whenever PHI_MODE=synthetic or no vendor is configured — see
app/transcription/factory.py. Also the only transcriber exercised by the
test suite; see the module docstring on app/transcription/openai.py for
why real vendor calls never run in CI.
"""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator

from app.transcription.base import (
    AudioChunk,
    AudioSource,
    Transcriber,
    TranscriptionEvent,
    TranscriptionRateLimitError,
    TranscriptionResult,
    TranscriptSegment,
    materialize_chunks,
)

# A deterministic, synthetic (no real patient involved) primary-care
# visit transcript. Fixed start/end offsets in seconds; speaker is None
# to match v1's non-diarizing reality (see base.py's TranscriptSegment
# docstring) even though a mock *could* fake diarization.
_FIXTURE_SEGMENTS: list[TranscriptSegment] = [
    TranscriptSegment(text="Good morning, what brings you in today?", start=0.0, end=3.2),
    TranscriptSegment(
        text="I've had a mild headache and some fatigue for about three days now.",
        start=3.2,
        end=8.6,
    ),
    TranscriptSegment(
        text="Any fever, nausea, or vision changes along with the headache?",
        start=8.6,
        end=12.9,
    ),
    TranscriptSegment(
        text="No fever that I've noticed, no nausea. Vision seems normal.",
        start=12.9,
        end=17.4,
    ),
    TranscriptSegment(
        text="Okay. Have you been drinking enough water and sleeping normally?",
        start=17.4,
        end=21.8,
    ),
    TranscriptSegment(
        text="Sleep's been disrupted, and honestly I probably haven't drunk much water.",
        start=21.8,
        end=28.0,
    ),
    TranscriptSegment(
        text="That's likely a factor. Let's check your blood pressure and go from there.",
        start=28.0,
        end=33.5,
    ),
]

_FIXTURE_FULL_TEXT = " ".join(segment.text for segment in _FIXTURE_SEGMENTS)
_FIXTURE_DURATION_SECONDS = _FIXTURE_SEGMENTS[-1].end


def _empty_result(language: str | None) -> TranscriptionResult:
    return TranscriptionResult(
        segments=[],
        full_text="",
        provider="mock",
        language=language,
        duration_seconds=0.0,
    )


def _fixture_result(language: str | None) -> TranscriptionResult:
    return TranscriptionResult(
        segments=list(_FIXTURE_SEGMENTS),
        full_text=_FIXTURE_FULL_TEXT,
        provider="mock",
        language=language,
        duration_seconds=_FIXTURE_DURATION_SECONDS,
    )


class MockTranscriber(Transcriber):
    """Deterministic transcriber fixture. Configurable to simulate
    latency, a rate-limit error, and empty audio — all via constructor
    flags, so tests never need real timing or a real vendor outage to
    exercise those paths.
    """

    provider_name = "mock"

    def __init__(
        self,
        *,
        latency_seconds: float = 0.0,
        simulate_rate_limit: bool = False,
        simulate_empty_audio: bool = False,
    ) -> None:
        self._latency_seconds = latency_seconds
        self._simulate_rate_limit = simulate_rate_limit
        self._simulate_empty_audio = simulate_empty_audio

    async def _apply_simulated_conditions(self) -> None:
        if self._latency_seconds:
            await asyncio.sleep(self._latency_seconds)
        if self._simulate_rate_limit:
            raise TranscriptionRateLimitError("mock: simulated rate limit exceeded")

    async def transcribe(
        self,
        chunks: AudioSource,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        resolved = await materialize_chunks(chunks)
        await self._apply_simulated_conditions()

        if self._simulate_empty_audio or not resolved:
            return _empty_result(language)
        return _fixture_result(language)

    async def stream(self, chunks: AsyncIterable[AudioChunk]) -> AsyncIterator[TranscriptionEvent]:
        resolved = await materialize_chunks(chunks)
        await self._apply_simulated_conditions()

        if self._simulate_empty_audio or not resolved:
            yield TranscriptionEvent(type="final", text="")
            return

        # A few partials, each appending one more fixture segment's worth
        # of text — a simple, deterministic stand-in for "the transcript
        # is still being built up live" — followed by exactly one final
        # event carrying the complete text.
        built_so_far = ""
        for segment in _FIXTURE_SEGMENTS[:-1]:
            built_so_far = f"{built_so_far} {segment.text}".strip()
            yield TranscriptionEvent(type="partial", text=built_so_far)

        yield TranscriptionEvent(type="final", text=_FIXTURE_FULL_TEXT)
