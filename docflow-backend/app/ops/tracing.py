"""PHI-safe LLM tracing via SELF-HOSTED Langfuse only (Phase 8).

HARD RULE: Langfuse Cloud is forbidden. Three independent layers refuse
it, so one bug in any single layer can't leak PHI to a third party:
  1. app/config.py's _validate_tracing_is_self_hosted — Settings itself
     cannot be constructed with NOTE_TRACING_ENABLED=true and no
     self-hosted LANGFUSE_HOST.
  2. app/security/startup_checks.py's run_startup_safety_checks —
     re-asserts the same rule at application startup.
  3. _build_langfuse_client below — refuses to construct a client at
     all if LANGFUSE_HOST is missing or points at Langfuse Cloud, even
     if something upstream ever changes.

Even self-hosted, a trace is still PHI-adjacent: treat it as inside the
same BAA boundary as everything else this service touches. By default,
prompt/completion CONTENT (transcript and note text) is NEVER attached
to a trace — only metadata (model, prompt_version, token/char counts,
latency, retry_count, degraded, outcome). TRACE_INCLUDE_CONTENT can
enable content for synthetic/dev debugging only; Settings refuses to
construct at all with TRACE_INCLUDE_CONTENT=true under PHI_MODE=real or
ENV=prod (see app/config.py and app/security/startup_checks.py).

Integrated at the note-generation ORCHESTRATION boundary
(app/services/note_service.py), not inside app/notes/openai.py's
per-attempt _trace_generation hook: this module traces ONCE per logical
generate_note_for_session() call (matching the audit-once-per-operation
convention app/services/audit_service.py established), not once per
retry attempt.

The langfuse SDK is imported lazily, only when NOTE_TRACING_ENABLED is
true — same "don't import a vendor SDK you're not using" discipline as
app/transcription/openai.py and app/notes/openai.py.
"""

import logging
import uuid
from functools import lru_cache
from typing import Any, Protocol

from app.config import Settings, get_settings
from app.notes.base import SoapNote

logger = logging.getLogger(__name__)


class TracingConfigurationError(Exception):
    """Refused to build a tracer — see module docstring for the three
    layers this backs up."""


class _LangfuseObservation(Protocol):
    def end(self) -> object: ...


class _LangfuseClient(Protocol):
    def start_observation(self, **kwargs: Any) -> _LangfuseObservation: ...
    def create_trace_id(self, *, seed: str | None = None) -> str: ...


def _build_langfuse_client(settings: Settings) -> _LangfuseClient:
    if not settings.LANGFUSE_HOST or "cloud.langfuse.com" in settings.LANGFUSE_HOST.lower():
        raise TracingConfigurationError(
            "LANGFUSE_HOST must be a self-hosted instance — refusing to build a tracer"
        )
    from langfuse import Langfuse  # lazy import — see module docstring

    # Langfuse's real start_observation is a keyword-only overload set
    # (one per as_type literal) that doesn't structurally match our
    # deliberately-simplified **kwargs Protocol above — narrowed to what
    # this module actually calls, same "SDK's real type is wider than
    # what we use" situation as app/notes/openai.py's
    # `# type: ignore[call-overload]` on its own SDK call.
    return Langfuse(  # type: ignore[return-value]
        host=settings.LANGFUSE_HOST,
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
    )


def _build_metadata(
    *,
    prompt_version: str,
    provider: str,
    model: str | None,
    transcript_text: str,
    soap: SoapNote | None,
    retry_count: int,
    degraded: bool,
    outcome: str,
    latency_seconds: float,
    include_content: bool,
) -> dict[str, object]:
    """PHI-safe by default: lengths, not text. See module docstring for
    when include_content may be true (synthetic/dev only, enforced at
    Settings-construction time, not here).
    """
    metadata: dict[str, object] = {
        "prompt_version": prompt_version,
        "provider": provider,
        "model": model,
        "retry_count": retry_count,
        "degraded": degraded,
        "outcome": outcome,
        "latency_seconds": round(latency_seconds, 3),
        "transcript_char_count": len(transcript_text),
    }
    if soap is not None:
        metadata["note_char_count"] = len(soap.full_text)
    if include_content:
        metadata["transcript_text"] = transcript_text
        if soap is not None:
            metadata["note_text"] = soap.full_text
    return metadata


class Tracer:
    """A no-op when NOTE_TRACING_ENABLED is false, so call sites never
    need an `if enabled` branch of their own.
    """

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.NOTE_TRACING_ENABLED
        self._include_content = settings.TRACE_INCLUDE_CONTENT
        self._client: _LangfuseClient | None = (
            _build_langfuse_client(settings) if self._enabled else None
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_note_generation(
        self,
        *,
        session_id: uuid.UUID,
        prompt_version: str,
        provider: str,
        model: str | None,
        transcript_text: str,
        soap: SoapNote | None,
        retry_count: int,
        degraded: bool,
        outcome: str,
        latency_seconds: float,
    ) -> None:
        if not self._enabled or self._client is None:
            return
        metadata = _build_metadata(
            prompt_version=prompt_version,
            provider=provider,
            model=model,
            transcript_text=transcript_text,
            soap=soap,
            retry_count=retry_count,
            degraded=degraded,
            outcome=outcome,
            latency_seconds=latency_seconds,
            include_content=self._include_content,
        )
        try:
            # A fire-and-forget, after-the-fact record: generation already
            # finished (successfully or not) by the time this is called,
            # so start+immediately end rather than wrapping the actual
            # vendor call — real duration is already in metadata's
            # latency_seconds. trace_id is deterministically seeded from
            # session_id (itself not PHI, just an internal identifier) so
            # repeated generations for the same session correlate in the
            # Langfuse UI without ever sending the session_id as a label
            # anywhere PHI-cardinality rules apply (see app/ops/metrics.py).
            trace_id = self._client.create_trace_id(seed=str(session_id))
            observation = self._client.start_observation(
                trace_context={"trace_id": trace_id},
                name="soap_note_generation",
                as_type="generation",
                model=model or provider,
                metadata=metadata,
            )
            observation.end()
        except Exception:
            # A trace backend hiccup must never break note generation —
            # this call sits after persistence has already happened.
            logger.exception("langfuse trace emission failed (non-fatal)")


@lru_cache
def get_tracer() -> Tracer:
    return Tracer(get_settings())
