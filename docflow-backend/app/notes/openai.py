"""OpenAINoteGenerator — the ONLY module in app/notes/ allowed to import
the `openai` SDK. See app/notes/base.py's module docstring for why that
boundary matters, tests/test_notes_isolation.py for the test (and
pyproject.toml's `flake8-tidy-imports` banned-api rule for the lint-level
enforcement) that keeps it true.

This is never exercised against the real network in CI — see
tests/test_notes_openai.py, which mocks the SDK client entirely. Real
generation from real (non-synthetic) transcript text additionally
requires a signed BAA *and* a Zero Data Retention agreement with OpenAI;
running vendor="openai" without both is a compliance violation, not just
a config mistake — see app/notes/factory.py's guardrails.

LLM tracing: if Settings.NOTE_TRACING_ENABLED is set, generation calls
are annotated for a SELF-HOSTED Langfuse instance only (see
app/config.py's _validate_tracing_is_self_hosted — Langfuse Cloud is
refused at startup because it would mean transcript/note PHI leaving this
process). No tracing SDK is wired in yet; _trace_generation is the single
call site a later phase hooks a self-hosted Langfuse client into, kept
PHI-safe (metadata only — see its docstring) from day one.
"""

import asyncio
import json
import logging
import random

import openai
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Settings
from app.notes.base import (
    NonEmptyStr,
    NoteAuthError,
    NoteContext,
    NoteGenerationError,
    NoteGenerator,
    NoteRateLimitError,
    NoteTimeoutError,
    NoteValidationError,
    SoapNote,
)
from app.notes.prompts.soap_primary_care_v1 import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class _RawSoapOutput(BaseModel):
    """The model's raw JSON response, validated strictly BEFORE it's
    allowed to become a SoapNote: exactly these four keys, nothing else,
    none empty. `extra="forbid"` is what makes "the model returned
    extra/missing keys" a validation failure (triggering a retry) rather
    than silently-ignored noise.
    """

    model_config = ConfigDict(extra="forbid")

    subjective: NonEmptyStr
    objective: NonEmptyStr
    assessment: NonEmptyStr
    plan: NonEmptyStr


def _translate_error(exc: Exception) -> NoteGenerationError:
    """Maps an openai SDK exception (or our own timeout) to one of the
    vendor-neutral exceptions callers are allowed to catch. Never called
    with, and never returns, a raw openai.* exception. Mirrors
    app/transcription/openai.py's _translate_error.
    """
    if isinstance(exc, TimeoutError):
        return NoteTimeoutError(str(exc) or "OpenAI note generation request timed out")
    if isinstance(exc, openai.APITimeoutError):
        return NoteTimeoutError(str(exc))
    if isinstance(exc, openai.AuthenticationError):
        return NoteAuthError(str(exc))
    if isinstance(exc, openai.RateLimitError):
        return NoteRateLimitError(str(exc))
    if isinstance(exc, openai.OpenAIError):
        return NoteGenerationError(str(exc))
    return NoteGenerationError(f"unexpected OpenAI note generation failure: {exc}")


def _retry_delay_seconds(attempt: int) -> float:
    """Exponential backoff with jitter: 0.5s, 1s, 2s, ... plus up to 250ms
    of random jitter to avoid a thundering herd on shared rate limits.
    Identical formula to app/transcription/openai.py's.
    """
    base = 0.5 * (2.0**attempt)
    return base + random.uniform(0, 0.25)


def _build_messages(
    transcript_text: str, context: NoteContext, *, previous_error: str | None
) -> list[dict[str, str]]:
    user_content = f"Transcript:\n{transcript_text}"
    if context.language:
        user_content += f"\n\n(Respond in {context.language}.)"
    if previous_error:
        # Fed back verbatim so the model can see exactly what was wrong
        # with its last attempt — pydantic's ValidationError message
        # names the offending field(s), which is enough signal to
        # self-correct without echoing any transcript/note content back.
        user_content += (
            f"\n\nYour previous response failed schema validation: {previous_error}\n"
            'Return ONLY valid JSON matching the schema: {"subjective": "...", '
            '"objective": "...", "assessment": "...", "plan": "..."} — no other '
            "keys, no prose outside the JSON."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _trace_generation(*, provider: str, model: str, attempt: int, outcome: str) -> None:
    """Hook point for self-hosted-Langfuse tracing (see module docstring).
    PHI-safe by construction: only ever pass metadata like this (provider,
    model, attempt count, outcome) — never transcript or note text.
    A no-op unless/until a later phase wires in the Langfuse client.
    """
    return


class OpenAINoteGenerator(NoteGenerator):
    provider_name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.OPENAI_API_KEY:
            # Defensive: the factory is the intended gate for this, but a
            # direct construction (e.g. in a test) should fail the same way.
            raise NoteAuthError("OPENAI_API_KEY is not configured")
        self._client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self._model = settings.OPENAI_NOTE_MODEL
        self._timeout_seconds = settings.NOTE_TIMEOUT_SECONDS
        self._max_retries = settings.NOTE_MAX_RETRIES
        self._tracing_enabled = settings.NOTE_TRACING_ENABLED

    async def generate_soap(self, transcript_text: str, context: NoteContext) -> SoapNote:
        previous_error: str | None = None
        last_error: NoteGenerationError | None = None

        for attempt in range(self._max_retries + 1):
            is_last_attempt = attempt == self._max_retries
            messages = _build_messages(transcript_text, context, previous_error=previous_error)

            try:
                response = await asyncio.wait_for(
                    # mypy can't resolve this SDK call's overload set (the
                    # model literal union crossed with our plain
                    # list[dict[str, str]] messages) — runtime behavior is
                    # covered by tests/test_notes_openai.py's mocked
                    # client instead. Same situation as
                    # app/transcription/openai.py's _transcribe_window.
                    self._client.chat.completions.create(  # type: ignore[call-overload]
                        model=self._model,
                        messages=messages,
                        response_format={"type": "json_object"},
                    ),
                    timeout=self._timeout_seconds,
                )
            except Exception as exc:  # translated immediately below, never re-raised as-is
                translated = _translate_error(exc)
                is_retryable = isinstance(translated, NoteRateLimitError | NoteTimeoutError)
                if self._tracing_enabled:
                    _trace_generation(
                        provider=self.provider_name,
                        model=self._model,
                        attempt=attempt,
                        outcome=type(translated).__name__,
                    )
                if not is_retryable or is_last_attempt:
                    self.last_retry_count = attempt
                    raise translated from exc
                last_error = translated
                logger.warning(
                    "openai note generation attempt failed, retrying",
                    extra={"attempt": attempt, "error_type": type(translated).__name__},
                )
                await asyncio.sleep(_retry_delay_seconds(attempt))
                continue

            raw_text = response.choices[0].message.content or ""
            try:
                parsed = json.loads(raw_text)
                raw_sections = _RawSoapOutput.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as exc:
                if self._tracing_enabled:
                    _trace_generation(
                        provider=self.provider_name,
                        model=self._model,
                        attempt=attempt,
                        outcome="validation_error",
                    )
                if is_last_attempt:
                    self.last_retry_count = attempt
                    raise NoteValidationError(
                        f"model output failed schema validation after "
                        f"{attempt + 1} attempt(s): {exc}"
                    ) from exc
                previous_error = str(exc)
                logger.warning(
                    "openai note output failed schema validation, retrying",
                    extra={"attempt": attempt},
                )
                continue

            if self._tracing_enabled:
                _trace_generation(
                    provider=self.provider_name,
                    model=self._model,
                    attempt=attempt,
                    outcome="success",
                )
            self.last_retry_count = attempt
            return SoapNote.compose(
                subjective=raw_sections.subjective,
                objective=raw_sections.objective,
                assessment=raw_sections.assessment,
                plan=raw_sections.plan,
                provider=self.provider_name,
                model=self._model,
            )

        # Unreachable in practice (the loop above always returns or
        # raises on its last iteration), but keeps mypy happy about the
        # function's return type — same pattern as
        # app/transcription/openai.py's _transcribe_window.
        assert last_error is not None
        raise last_error
