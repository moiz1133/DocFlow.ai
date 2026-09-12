"""OpenAINoteGenerator unit tests — network fully mocked, no real API
calls.

This is one of two test modules allowed to import the `openai` SDK
directly (to construct realistic vendor exceptions) — see
tests/test_transcription_isolation.py's docstring for why that's scoped
to app/ and doesn't apply to tests/.
"""

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx2
import openai as openai_sdk
import pytest

import app.notes.openai as notes_openai_module
from app.config import Settings
from app.notes.base import (
    NoteAuthError,
    NoteContext,
    NoteRateLimitError,
    NoteTimeoutError,
    NoteValidationError,
)
from app.notes.openai import OpenAINoteGenerator

_CONTEXT = NoteContext(specialty="primary_care")

_VALID_SECTIONS: dict[str, object] = {
    "subjective": "Patient reports a mild headache and fatigue.",
    "objective": "Blood pressure checked at this visit.",
    "assessment": "Likely tension-type headache.",
    "plan": "Increase fluids, follow up in one week.",
}


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


def _response(payload: dict[str, object]) -> _FakeResponse:
    return _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content=json.dumps(payload)))])


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
        "OPENAI_API_KEY": "sk-fake-test-key",
        "NOTE_MAX_RETRIES": 2,
        "NOTE_TIMEOUT_SECONDS": 5.0,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def _generator(**setting_overrides: object) -> OpenAINoteGenerator:
    return OpenAINoteGenerator(_settings(**setting_overrides))


def _fake_response_object(status_code: int) -> httpx2.Response:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    return httpx2.Response(status_code, request=request)


def test_construction_without_api_key_raises_note_auth_error() -> None:
    with pytest.raises(NoteAuthError):
        OpenAINoteGenerator(_settings(OPENAI_API_KEY=None))


async def test_generate_soap_returns_valid_note_on_first_attempt() -> None:
    generator = _generator()
    mock_create = AsyncMock(return_value=_response(_VALID_SECTIONS))
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    note = await generator.generate_soap("some transcript", _CONTEXT)

    assert note.provider == "openai"
    assert note.subjective == _VALID_SECTIONS["subjective"]
    assert note.objective == _VALID_SECTIONS["objective"]
    assert note.assessment == _VALID_SECTIONS["assessment"]
    assert note.plan == _VALID_SECTIONS["plan"]
    assert generator.last_retry_count == 0
    mock_create.assert_awaited_once()


async def test_malformed_json_then_valid_succeeds_with_retry_recorded() -> None:
    generator = _generator()
    responses = [
        _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content="not json"))]),
        _response(_VALID_SECTIONS),
    ]
    mock_create = AsyncMock(side_effect=responses)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    note = await generator.generate_soap("some transcript", _CONTEXT)

    assert note.provider == "openai"
    assert generator.last_retry_count == 1
    assert mock_create.await_count == 2


async def test_extra_keys_in_response_triggers_a_retry() -> None:
    generator = _generator()
    responses = [
        _response({**_VALID_SECTIONS, "diagnosis_code": "R51"}),
        _response(_VALID_SECTIONS),
    ]
    mock_create = AsyncMock(side_effect=responses)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    note = await generator.generate_soap("some transcript", _CONTEXT)

    assert note.subjective == _VALID_SECTIONS["subjective"]
    assert mock_create.await_count == 2


async def test_missing_key_in_response_triggers_a_retry() -> None:
    generator = _generator()
    incomplete = {k: v for k, v in _VALID_SECTIONS.items() if k != "plan"}
    responses = [_response(incomplete), _response(_VALID_SECTIONS)]
    mock_create = AsyncMock(side_effect=responses)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    note = await generator.generate_soap("some transcript", _CONTEXT)

    assert note.plan == _VALID_SECTIONS["plan"]
    assert mock_create.await_count == 2


async def test_empty_section_triggers_a_retry() -> None:
    generator = _generator()
    responses = [_response({**_VALID_SECTIONS, "plan": "   "}), _response(_VALID_SECTIONS)]
    mock_create = AsyncMock(side_effect=responses)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    note = await generator.generate_soap("some transcript", _CONTEXT)

    assert note.plan == _VALID_SECTIONS["plan"]


async def test_exhausting_retries_on_malformed_output_raises_note_validation_error() -> None:
    generator = _generator(NOTE_MAX_RETRIES=1)
    mock_create = AsyncMock(
        return_value=_FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content="not json"))])
    )
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    with pytest.raises(NoteValidationError):
        await generator.generate_soap("some transcript", _CONTEXT)

    assert mock_create.await_count == 2  # initial attempt + 1 retry
    assert generator.last_retry_count == 1


async def test_previous_error_is_fed_back_into_the_next_attempt() -> None:
    """The retry prompt must reference the validation failure so the
    model can self-correct — see app/notes/openai.py's _build_messages.
    """
    generator = _generator()
    responses = [
        _FakeResponse(choices=[_FakeChoice(message=_FakeMessage(content="not json"))]),
        _response(_VALID_SECTIONS),
    ]
    mock_create = AsyncMock(side_effect=responses)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    await generator.generate_soap("some transcript", _CONTEXT)

    second_call_messages = mock_create.await_args_list[1].kwargs["messages"]
    user_message = next(m["content"] for m in second_call_messages if m["role"] == "user")
    assert "failed schema validation" in user_message


async def test_authentication_error_is_translated_and_never_retried() -> None:
    generator = _generator()
    error = openai_sdk.AuthenticationError(
        "invalid api key", response=_fake_response_object(401), body=None
    )
    mock_create = AsyncMock(side_effect=error)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    with pytest.raises(NoteAuthError):
        await generator.generate_soap("some transcript", _CONTEXT)

    mock_create.assert_awaited_once()


async def test_rate_limit_error_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notes_openai_module, "_retry_delay_seconds", lambda attempt: 0.0)
    generator = _generator()

    error = openai_sdk.RateLimitError("slow down", response=_fake_response_object(429), body=None)
    responses = [error, _response(_VALID_SECTIONS)]
    mock_create = AsyncMock(side_effect=responses)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    note = await generator.generate_soap("some transcript", _CONTEXT)

    assert note.provider == "openai"
    assert mock_create.await_count == 2


async def test_rate_limit_error_exhausts_retries_and_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notes_openai_module, "_retry_delay_seconds", lambda attempt: 0.0)
    generator = _generator(NOTE_MAX_RETRIES=2)

    error = openai_sdk.RateLimitError("slow down", response=_fake_response_object(429), body=None)
    mock_create = AsyncMock(side_effect=error)
    generator._client.chat.completions.create = mock_create  # type: ignore[method-assign]

    with pytest.raises(NoteRateLimitError):
        await generator.generate_soap("some transcript", _CONTEXT)

    assert mock_create.await_count == 3  # initial attempt + 2 retries


async def test_slow_response_is_translated_to_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notes_openai_module, "_retry_delay_seconds", lambda attempt: 0.0)
    generator = _generator(NOTE_TIMEOUT_SECONDS=0.01, NOTE_MAX_RETRIES=1)

    async def _slow_create(**kwargs: object) -> _FakeResponse:
        await asyncio.sleep(0.2)
        return _response(_VALID_SECTIONS)

    generator._client.chat.completions.create = _slow_create  # type: ignore[method-assign,assignment]

    with pytest.raises(NoteTimeoutError):
        await generator.generate_soap("some transcript", _CONTEXT)
