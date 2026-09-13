"""PHI-safe self-hosted-only Langfuse tracing (Phase 8) —
app/ops/tracing.py. The Langfuse-cloud-refused-at-startup and
TRACE_INCLUDE_CONTENT-refused-under-real/prod rules themselves are
tested in tests/test_startup_checks.py (they're startup-check/Settings
concerns); this file covers Tracer's own behavior: metadata-only by
default, content only when explicitly enabled, and never touching the
langfuse SDK at all when tracing is disabled.
"""

import uuid

import pytest

from app.config import Settings, get_settings
from app.notes.base import SoapNote
from app.ops.tracing import Tracer


def _settings(**overrides: object) -> Settings:
    base = get_settings().model_copy()
    return base.model_copy(update=overrides)


def test_disabled_tracer_never_builds_a_langfuse_client() -> None:
    tracer = Tracer(_settings(NOTE_TRACING_ENABLED=False))
    assert tracer.enabled is False
    # No exception, no client — record_note_generation is a pure no-op.
    tracer.record_note_generation(
        session_id=uuid.uuid4(),
        prompt_version="v1",
        provider="mock",
        model=None,
        transcript_text="patient reports chest pain",
        soap=None,
        retry_count=0,
        degraded=False,
        outcome="success",
        latency_seconds=0.1,
    )


class _RecordingObservation:
    def __init__(self) -> None:
        self.ended = False

    def end(self) -> "_RecordingObservation":
        self.ended = True
        return self


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_trace_id(self, *, seed: str | None = None) -> str:
        return "fake-trace-id"

    def start_observation(self, **kwargs: object) -> _RecordingObservation:
        self.calls.append(kwargs)
        return _RecordingObservation()


def _enabled_tracer(
    *, include_content: bool, monkeypatch: pytest.MonkeyPatch
) -> tuple[Tracer, _RecordingClient]:
    import app.ops.tracing as tracing_module

    fake_client = _RecordingClient()
    monkeypatch.setattr(tracing_module, "_build_langfuse_client", lambda settings: fake_client)
    tracer = Tracer(
        _settings(
            NOTE_TRACING_ENABLED=True,
            LANGFUSE_HOST="https://langfuse.internal.example.com",
            TRACE_INCLUDE_CONTENT=include_content,
            PHI_MODE="synthetic",
            ENV="dev",
        )
    )
    return tracer, fake_client


def test_enabled_tracer_records_metadata_only_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer, fake_client = _enabled_tracer(include_content=False, monkeypatch=monkeypatch)
    soap = SoapNote.compose(
        subjective="patient reports chest pain",
        objective="alert, oriented",
        assessment="likely musculoskeletal",
        plan="follow up in 1 week",
        provider="mock",
    )

    tracer.record_note_generation(
        session_id=uuid.uuid4(),
        prompt_version="soap_primary_care_v1",
        provider="mock",
        model=None,
        transcript_text="patient reports chest pain and shortness of breath",
        soap=soap,
        retry_count=1,
        degraded=False,
        outcome="success",
        latency_seconds=0.42,
    )

    assert len(fake_client.calls) == 1
    metadata = fake_client.calls[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["prompt_version"] == "soap_primary_care_v1"
    assert metadata["retry_count"] == 1
    assert metadata["transcript_char_count"] == len(
        "patient reports chest pain and shortness of breath"
    )
    assert metadata["note_char_count"] == len(soap.full_text)
    # PHI-free by default: no transcript/note TEXT anywhere in the call.
    assert "transcript_text" not in metadata
    assert "note_text" not in metadata
    assert "chest pain" not in str(fake_client.calls[0])


def test_enabled_tracer_with_include_content_attaches_text(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer, fake_client = _enabled_tracer(include_content=True, monkeypatch=monkeypatch)
    soap = SoapNote.compose(
        subjective="patient reports chest pain",
        objective="alert, oriented",
        assessment="likely musculoskeletal",
        plan="follow up in 1 week",
        provider="mock",
    )

    tracer.record_note_generation(
        session_id=uuid.uuid4(),
        prompt_version="soap_primary_care_v1",
        provider="mock",
        model=None,
        transcript_text="patient reports chest pain",
        soap=soap,
        retry_count=0,
        degraded=False,
        outcome="success",
        latency_seconds=0.1,
    )

    metadata = fake_client.calls[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["transcript_text"] == "patient reports chest pain"
    assert metadata["note_text"] == soap.full_text


def test_tracer_emission_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ops.tracing as tracing_module

    class _BoomClient:
        def create_trace_id(self, *, seed: str | None = None) -> str:
            raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr(tracing_module, "_build_langfuse_client", lambda settings: _BoomClient())
    tracer = Tracer(
        _settings(
            NOTE_TRACING_ENABLED=True,
            LANGFUSE_HOST="https://langfuse.internal.example.com",
            PHI_MODE="synthetic",
            ENV="dev",
        )
    )

    # Must not raise — a trace-backend hiccup can never break note generation.
    tracer.record_note_generation(
        session_id=uuid.uuid4(),
        prompt_version="v1",
        provider="mock",
        model=None,
        transcript_text="x",
        soap=None,
        retry_count=0,
        degraded=True,
        outcome="NoteValidationError",
        latency_seconds=0.1,
    )
