"""Contract tests for MockNoteGenerator against the vendor-neutral
NoteGenerator interface — see app/notes/base.py and app/notes/mock.py.
"""

import pytest

from app.notes.base import NoteContext, NoteRateLimitError, NoteTimeoutError, NoteValidationError
from app.notes.mock import MockNoteGenerator

_CONTEXT = NoteContext(specialty="primary_care")


async def test_generate_soap_returns_a_well_formed_note() -> None:
    generator = MockNoteGenerator()

    note = await generator.generate_soap("some transcript text", _CONTEXT)

    assert note.provider == "mock"
    assert note.subjective.strip()
    assert note.objective.strip()
    assert note.assessment.strip()
    assert note.plan.strip()


async def test_full_text_is_composed_from_the_four_sections() -> None:
    generator = MockNoteGenerator()
    note = await generator.generate_soap("some transcript text", _CONTEXT)

    assert note.subjective in note.full_text
    assert note.objective in note.full_text
    assert note.assessment in note.full_text
    assert note.plan in note.full_text


async def test_default_generation_succeeds_on_the_first_attempt() -> None:
    generator = MockNoteGenerator()
    await generator.generate_soap("some transcript text", _CONTEXT)

    assert generator.last_retry_count == 0


async def test_malformed_once_then_valid_succeeds_with_retry_recorded() -> None:
    generator = MockNoteGenerator(simulate_malformed_once=True)

    note = await generator.generate_soap("some transcript text", _CONTEXT)

    assert note.provider == "mock"
    assert generator.last_retry_count == 1


async def test_always_malformed_raises_validation_error_after_exhausting_attempts() -> None:
    generator = MockNoteGenerator(simulate_always_malformed=True)

    with pytest.raises(NoteValidationError):
        await generator.generate_soap("some transcript text", _CONTEXT)


async def test_simulated_rate_limit_surfaces_as_typed_error_not_a_vendor_error() -> None:
    generator = MockNoteGenerator(simulate_rate_limit=True)

    with pytest.raises(NoteRateLimitError):
        await generator.generate_soap("some transcript text", _CONTEXT)


async def test_simulated_timeout_surfaces_as_typed_error() -> None:
    generator = MockNoteGenerator(simulate_timeout=True)

    with pytest.raises(NoteTimeoutError):
        await generator.generate_soap("some transcript text", _CONTEXT)
