"""Vendor selection for note generation — mirrors
app/transcription/factory.py's structure and guardrails exactly (see that
module's docstring for the rationale behind each one).

`build_note_generator(settings)` is the pure, unmemoized selection logic
— easy to call with a custom Settings in tests. `get_note_generator()` is
the process-wide singleton wrapper application code should use, matching
the zero-arg `@lru_cache` convention already established by
app/db/session.py's get_engine()/get_sessionmaker() and
app/transcription/factory.py's get_transcriber() (the spec for this
module described a `get_note_generator(settings)` signature; this
deviates the same documented way Phase 4 did, for the same reason:
pydantic Settings instances aren't hashable, so they can't be an
@lru_cache key).

The `openai` module is imported lazily, only inside the "openai" branch —
selecting vendor="mock" (the default, and the only option when
PHI_MODE=synthetic) never touches the SDK at all. See
tests/test_notes_isolation.py.
"""

import logging
from functools import lru_cache

from app.config import Settings, get_settings
from app.notes.base import NoteGenerator
from app.notes.mock import MockNoteGenerator

logger = logging.getLogger(__name__)


class NoteGeneratorConfigurationError(Exception):
    """Raised when NOTE_GENERATOR_VENDOR is misconfigured for the current
    environment — always meant to fail application startup, not be caught
    and worked around at request time.
    """


def build_note_generator(settings: Settings) -> NoteGenerator:
    vendor = settings.NOTE_GENERATOR_VENDOR

    # Real vendors only ever handle real, BAA-covered transcript text.
    # Synthetic data (dev/test) must never leave the process, regardless
    # of what NOTE_GENERATOR_VENDOR is set to — this is not overridable.
    if settings.PHI_MODE == "synthetic":
        vendor = "mock"

    if settings.ENV == "prod" and vendor == "mock":
        raise NoteGeneratorConfigurationError(
            "NOTE_GENERATOR_VENDOR resolved to 'mock' with ENV=prod — a real "
            "vendor is required in production. If PHI_MODE=synthetic is "
            "forcing this, that combination is itself invalid in prod."
        )

    if vendor == "mock":
        return MockNoteGenerator()

    if vendor == "openai":
        if not settings.OPENAI_API_KEY:
            raise NoteGeneratorConfigurationError(
                "NOTE_GENERATOR_VENDOR=openai requires OPENAI_API_KEY to be set"
            )
        # NOTE: real (non-synthetic) transcript text must not reach
        # OpenAI without a signed BAA *and* a Zero Data Retention
        # agreement in place — this factory has no way to verify that
        # operationally, so it is a deployment/process precondition, not
        # a check this code can make.
        from app.notes.openai import OpenAINoteGenerator

        return OpenAINoteGenerator(settings)

    raise NoteGeneratorConfigurationError(f"Unknown NOTE_GENERATOR_VENDOR: {vendor!r}")


@lru_cache
def get_note_generator() -> NoteGenerator:
    generator = build_note_generator(get_settings())
    logger.info("note generator selected", extra={"provider": generator.provider_name})
    return generator
