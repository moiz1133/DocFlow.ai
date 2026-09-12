"""OpenAITranscriber unit tests — network fully mocked, no real API calls.

This is the one test module allowed to import the `openai` SDK directly
(to construct realistic vendor exceptions) — see
test_transcription_isolation.py's docstring for why that's scoped to
app/ and doesn't apply to tests/.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx2
import openai as openai_sdk
import pytest

import app.transcription.openai as openai_module
from app.config import Settings
from app.transcription.base import (
    AudioChunk,
    TranscriptionAuthError,
    TranscriptionRateLimitError,
    TranscriptionTimeoutError,
)
from app.transcription.openai import OpenAITranscriber


@dataclass
class _FakeSegment:
    text: str
    start: float | None = None
    end: float | None = None


@dataclass
class _FakeResponse:
    text: str
    language: str | None = None
    duration: float | None = None
    segments: list[_FakeSegment] | None = None


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
        "OPENAI_API_KEY": "sk-fake-test-key",
        "TRANSCRIBE_MAX_RETRIES": 2,
        "TRANSCRIBE_TIMEOUT_SECONDS": 5.0,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def _transcriber(**setting_overrides: object) -> OpenAITranscriber:
    return OpenAITranscriber(_settings(**setting_overrides))


def _fake_response_object(status_code: int) -> httpx2.Response:
    request = httpx2.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    return httpx2.Response(status_code, request=request)


async def test_transcribe_maps_response_fields_onto_result() -> None:
    transcriber = _transcriber()
    fake_response = _FakeResponse(
        text="hello world",
        language="en",
        duration=5.0,
        segments=[_FakeSegment(text="hello world", start=0.0, end=5.0)],
    )
    transcriber._client.audio.transcriptions.create = AsyncMock(return_value=fake_response)  # type: ignore[method-assign]

    result = await transcriber.transcribe([AudioChunk(data=b"x" * 100, mime_type="audio/wav")])

    assert result.provider == "openai"
    assert result.full_text == "hello world"
    assert result.language == "en"
    assert result.duration_seconds == 5.0
    assert len(result.segments) == 1
    assert result.segments[0].speaker is None
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == 5.0


async def test_transcribe_with_no_segments_in_response_falls_back_to_one_segment() -> None:
    """Some models/response_formats only return `text`, no `segments`."""
    transcriber = _transcriber()
    fake_response = _FakeResponse(text="just text, no segments", duration=3.0, segments=None)
    transcriber._client.audio.transcriptions.create = AsyncMock(return_value=fake_response)  # type: ignore[method-assign]

    result = await transcriber.transcribe([AudioChunk(data=b"x" * 10, mime_type="audio/wav")])

    assert len(result.segments) == 1
    assert result.segments[0].text == "just text, no segments"
    assert result.segments[0].speaker is None


async def test_transcribe_with_empty_input_never_calls_the_api() -> None:
    transcriber = _transcriber()
    mock_create = AsyncMock()
    transcriber._client.audio.transcriptions.create = mock_create  # type: ignore[method-assign]

    result = await transcriber.transcribe([])

    assert result.segments == []
    assert result.full_text == ""
    mock_create.assert_not_called()


async def test_transcribe_splits_into_windows_by_byte_size_and_stitches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_module, "_MAX_WINDOW_BYTES", 10)
    transcriber = _transcriber()

    # 4 chunks of 5 bytes each, 10-byte window cap -> two windows of two
    # chunks apiece.
    chunks = [AudioChunk(data=b"12345", mime_type="audio/wav") for _ in range(4)]

    responses = [
        _FakeResponse(
            text="part one",
            duration=2.0,
            segments=[_FakeSegment(text="part one", start=0.0, end=2.0)],
        ),
        _FakeResponse(
            text="part two",
            duration=3.0,
            segments=[_FakeSegment(text="part two", start=2.0, end=5.0)],
        ),
    ]
    mock_create = AsyncMock(side_effect=responses)
    transcriber._client.audio.transcriptions.create = mock_create  # type: ignore[method-assign]

    result = await transcriber.transcribe(chunks)

    assert mock_create.call_count == 2
    assert result.full_text == "part one part two"
    assert result.duration_seconds == 5.0
    assert [segment.text for segment in result.segments] == ["part one", "part two"]


async def test_authentication_error_is_translated_and_never_retried() -> None:
    transcriber = _transcriber()
    error = openai_sdk.AuthenticationError(
        "invalid api key", response=_fake_response_object(401), body=None
    )
    mock_create = AsyncMock(side_effect=error)
    transcriber._client.audio.transcriptions.create = mock_create  # type: ignore[method-assign]

    with pytest.raises(TranscriptionAuthError):
        await transcriber.transcribe([AudioChunk(data=b"x", mime_type="audio/wav")])

    assert mock_create.call_count == 1


async def test_rate_limit_error_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_module, "_retry_delay_seconds", lambda attempt: 0.0)
    transcriber = _transcriber()

    error = openai_sdk.RateLimitError("slow down", response=_fake_response_object(429), body=None)
    success = _FakeResponse(
        text="ok now", duration=1.0, segments=[_FakeSegment(text="ok now", start=0.0, end=1.0)]
    )
    mock_create = AsyncMock(side_effect=[error, success])
    transcriber._client.audio.transcriptions.create = mock_create  # type: ignore[method-assign]

    result = await transcriber.transcribe([AudioChunk(data=b"x", mime_type="audio/wav")])

    assert result.full_text == "ok now"
    assert mock_create.call_count == 2


async def test_rate_limit_error_exhausts_retries_and_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_module, "_retry_delay_seconds", lambda attempt: 0.0)
    transcriber = _transcriber(TRANSCRIBE_MAX_RETRIES=2)

    error = openai_sdk.RateLimitError("slow down", response=_fake_response_object(429), body=None)
    mock_create = AsyncMock(side_effect=error)
    transcriber._client.audio.transcriptions.create = mock_create  # type: ignore[method-assign]

    with pytest.raises(TranscriptionRateLimitError):
        await transcriber.transcribe([AudioChunk(data=b"x", mime_type="audio/wav")])

    assert mock_create.call_count == 3  # initial attempt + 2 retries


async def test_slow_response_is_translated_to_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_module, "_retry_delay_seconds", lambda attempt: 0.0)
    transcriber = _transcriber(TRANSCRIBE_TIMEOUT_SECONDS=0.01, TRANSCRIBE_MAX_RETRIES=1)

    async def _slow_create(**kwargs: object) -> _FakeResponse:
        await asyncio.sleep(0.2)
        return _FakeResponse(text="too slow")

    transcriber._client.audio.transcriptions.create = _slow_create  # type: ignore[method-assign,assignment]

    with pytest.raises(TranscriptionTimeoutError):
        await transcriber.transcribe([AudioChunk(data=b"x", mime_type="audio/wav")])


async def test_stream_emulation_yields_one_final_event_per_window() -> None:
    transcriber = OpenAITranscriber(_settings(), stream_window_chunk_count=2)

    responses = [
        _FakeResponse(
            text="window one",
            duration=2.0,
            segments=[_FakeSegment(text="window one", start=0.0, end=2.0)],
        ),
        _FakeResponse(
            text="window two",
            duration=2.0,
            segments=[_FakeSegment(text="window two", start=2.0, end=4.0)],
        ),
    ]
    mock_create = AsyncMock(side_effect=responses)
    transcriber._client.audio.transcriptions.create = mock_create  # type: ignore[method-assign]

    async def _chunks() -> AsyncIterator[AudioChunk]:
        for _ in range(4):
            yield AudioChunk(data=b"x", mime_type="audio/wav")

    events = [event async for event in transcriber.stream(_chunks())]

    assert len(events) == 2
    assert all(event.type == "final" for event in events)
    assert events[0].text == "window one"
    assert events[1].text == "window two"
    assert events[0].segment is not None
    assert events[0].segment.speaker is None
