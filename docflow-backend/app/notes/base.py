"""Vendor-neutral SOAP note generation interface.

Nothing outside app/notes/ may import a vendor SDK — the same isolation
rule as app/transcription/ (see that package's base.py for the rationale;
tests/test_notes_isolation.py is the runtime-verified guardrail here, and
pyproject.toml's flake8-tidy-imports banned-api rule is the lint-time
one). Callers (app/services/note_service.py) depend only on the types and
abstract class defined here; app/notes/factory.py selects a concrete
implementation.
"""

from abc import ABC, abstractmethod
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Vendor-neutral data types
# ---------------------------------------------------------------------------


def _require_nonempty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


# Shared by SoapNote's four sections here and _RawSoapOutput in
# app/notes/openai.py, so "no empty section" is enforced identically
# whether the text came from a vendor response or was composed by a mock.
NonEmptyStr = Annotated[str, AfterValidator(_require_nonempty)]

Specialty = Literal["primary_care"]


class NoteContext(BaseModel):
    """Per-generation context threaded into the prompt. `specialty` is
    locked to "primary_care" for v1 (see the module docstring on
    app/notes/prompts/soap_primary_care_v1.py). `clinician_preferences`
    is an unused placeholder today (formatting hints, terminology) so a
    later phase can populate it with zero interface changes.
    """

    specialty: Specialty = "primary_care"
    language: str | None = None
    clinician_preferences: dict[str, str] = Field(default_factory=dict)


def _compose_full_text(subjective: str, objective: str, assessment: str, plan: str) -> str:
    return (
        f"Subjective: {subjective}\n\n"
        f"Objective: {objective}\n\n"
        f"Assessment: {assessment}\n\n"
        f"Plan: {plan}"
    )


class SoapNote(BaseModel):
    """The vendor-neutral SOAP note shape. Every NoteGenerator
    implementation, mock included, returns exactly this — persistence
    never needs to know which vendor produced a note.

    Strict by construction: all four sections are required and non-empty
    (NonEmptyStr), and `extra="forbid"` means a caller can't accidentally
    smuggle extra fields through. This is deliberately not what validates
    a vendor's *raw* JSON response, though — an implementation must
    parse/validate that separately (see app/notes/openai.py's
    _RawSoapOutput) before ever reaching SoapNote.compose, so "the model
    returned extra/missing keys" is rejected before construction, not
    silently dropped here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subjective: NonEmptyStr
    objective: NonEmptyStr
    assessment: NonEmptyStr
    plan: NonEmptyStr
    full_text: NonEmptyStr
    provider: str
    model: str | None = None

    @classmethod
    def compose(
        cls,
        *,
        subjective: str,
        objective: str,
        assessment: str,
        plan: str,
        provider: str,
        model: str | None = None,
    ) -> "SoapNote":
        """The one place full_text gets built from the four sections, so
        every implementation constructs it identically.
        """
        return cls(
            subjective=subjective,
            objective=objective,
            assessment=assessment,
            plan=plan,
            full_text=_compose_full_text(subjective, objective, assessment, plan),
            provider=provider,
            model=model,
        )


# ---------------------------------------------------------------------------
# Typed exceptions — vendor SDK errors must never escape an implementation
# ---------------------------------------------------------------------------


class NoteGenerationError(Exception):
    """Base for every note-generation failure. Callers should catch this
    (or a subclass) and never a vendor SDK exception directly —
    implementations are required to translate.
    """


class NoteAuthError(NoteGenerationError):
    """The vendor rejected our credentials."""


class NoteRateLimitError(NoteGenerationError):
    """The vendor rate-limited this request."""


class NoteTimeoutError(NoteGenerationError):
    """The vendor call exceeded the configured timeout."""


class NoteValidationError(NoteGenerationError):
    """The model's output could not be validated as a SoapNote, even
    after all configured retries.
    """


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class NoteGenerator(ABC):
    """Vendor-neutral SOAP note generation interface.

    `provider_name` is a plain string (e.g. "mock", "openai") logged at
    startup and stamped onto every SoapNote — never content.

    `last_retry_count` is set by generate_soap() after each call (0 =
    succeeded on the first attempt) and read by
    app/services/note_service.py for the audit trail. It's a plain
    instance attribute rather than part of SoapNote's schema because
    it's generation metadata, not part of the note itself — see
    SoapNote's docstring.
    """

    provider_name: str
    last_retry_count: int = 0

    @abstractmethod
    async def generate_soap(self, transcript_text: str, context: NoteContext) -> SoapNote:
        """Generates a SOAP note from (ideally already-scrubbed, see
        app/notes/scrub.py) transcript text. Raises a NoteGenerationError
        subclass on failure — never returns a partial/invalid note.
        """
        raise NotImplementedError


__all__ = [
    "NonEmptyStr",
    "NoteAuthError",
    "NoteContext",
    "NoteGenerationError",
    "NoteGenerator",
    "NoteRateLimitError",
    "NoteTimeoutError",
    "NoteValidationError",
    "SoapNote",
    "Specialty",
]
