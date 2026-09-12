"""Interface contract tests, run against MockTranscriber — the only
transcriber this suite ever talks to. See test_transcription_isolation.py
for the guarantee that nothing here (or anywhere outside
app/transcription/openai.py) ever imports the openai SDK.
"""

import time
from collections.abc import AsyncIterator

import pytest

from app.transcription.base import AudioChunk, TranscriptionRateLimitError
from app.transcription.mock import MockTranscriber

SAMPLE_CHUNKS = [AudioChunk(data=b"fake-audio-bytes", mime_type="audio/wav", sample_rate=16000)]


async def _chunk_stream() -> AsyncIterator[AudioChunk]:
    for chunk in SAMPLE_CHUNKS:
        yield chunk


async def test_transcribe_returns_a_well_formed_result() -> None:
    result = await MockTranscriber().transcribe(SAMPLE_CHUNKS)

    assert result.provider == "mock"
    assert result.full_text
    assert result.segments
    assert result.duration_seconds is not None
    assert result.duration_seconds > 0

    for segment in result.segments:
        assert isinstance(segment.text, str)
        assert segment.text
        assert segment.speaker is None or isinstance(segment.speaker, str)
        assert segment.start is None or isinstance(segment.start, float)
        assert segment.end is None or isinstance(segment.end, float)


async def test_transcribe_accepts_async_iterable_the_same_as_a_list() -> None:
    from_list = await MockTranscriber().transcribe(SAMPLE_CHUNKS)
    from_async = await MockTranscriber().transcribe(_chunk_stream())

    assert from_list.full_text == from_async.full_text
    assert len(from_list.segments) == len(from_async.segments)


async def test_transcribe_passes_through_requested_language() -> None:
    result = await MockTranscriber().transcribe(SAMPLE_CHUNKS, language="es")
    assert result.language == "es"


async def test_segments_match_the_phase_2_transcript_jsonb_shape() -> None:
    """Transcript.segments (Phase 2) is a JSONB array of
    {speaker, start, end, text} dicts — every segment must serialize to
    exactly that shape, no more, no fewer keys.
    """
    result = await MockTranscriber().transcribe(SAMPLE_CHUNKS)

    for segment in result.segments:
        as_dict = segment.to_jsonb()
        assert set(as_dict.keys()) == {"speaker", "start", "end", "text"}


async def test_transcribe_with_no_chunks_returns_empty_result() -> None:
    result = await MockTranscriber().transcribe([])
    assert result.segments == []
    assert result.full_text == ""


async def test_simulate_empty_audio_forces_empty_result_even_with_chunks() -> None:
    result = await MockTranscriber(simulate_empty_audio=True).transcribe(SAMPLE_CHUNKS)
    assert result.segments == []
    assert result.full_text == ""


async def test_simulate_latency_actually_delays() -> None:
    start = time.monotonic()
    await MockTranscriber(latency_seconds=0.05).transcribe(SAMPLE_CHUNKS)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05


async def test_simulated_rate_limit_surfaces_as_typed_error_not_a_vendor_error() -> None:
    """The whole point of the interface: callers only ever need to catch
    TranscriptionRateLimitError, never a vendor-specific exception —
    proven here even though the mock has no vendor SDK to raise from.
    """
    with pytest.raises(TranscriptionRateLimitError):
        await MockTranscriber(simulate_rate_limit=True).transcribe(SAMPLE_CHUNKS)


async def test_stream_yields_partials_then_exactly_one_final() -> None:
    events = [event async for event in MockTranscriber().stream(_chunk_stream())]

    assert events
    assert events[-1].type == "final"
    assert all(event.type == "partial" for event in events[:-1])
    assert events[-1].text  # the final event carries the full transcript


async def test_stream_with_empty_audio_yields_a_single_empty_final() -> None:
    events = [
        event async for event in MockTranscriber(simulate_empty_audio=True).stream(_chunk_stream())
    ]
    assert len(events) == 1
    assert events[0].type == "final"
    assert events[0].text == ""


async def test_stream_propagates_simulated_rate_limit() -> None:
    with pytest.raises(TranscriptionRateLimitError):
        async for _event in MockTranscriber(simulate_rate_limit=True).stream(_chunk_stream()):
            pass
