"""Lightweight structural guard: a service-layer function that reads a
PHI-bearing model (Transcript/Note — fetched via `select(...)` or
`db.get(...)`) must also call one of the audit choke points
(record_phi_access / its historically-named wrapper record_ingestion_event
— see app/services/audit_service.py's module docstring) somewhere in its
own body.

This is a heuristic, not a proof: it can't verify the audit call
actually fires on the code path that returns PHI, only that the function
mentions both "reads a PHI model" and "writes an audit entry" somewhere
in its source. Real behavioral coverage lives in
tests/test_note_service.py's test_note_generation_audits_the_transcript_read
and tests/test_sessions_finalize.py's audit tests — this is the belt to
that suspenders, the same "cheap static check alongside real tests"
relationship pyproject.toml's banned-api rule has to
tests/test_transcription_isolation.py.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_FILES = (
    REPO_ROOT / "app" / "services" / "transcription_service.py",
    REPO_ROOT / "app" / "services" / "note_service.py",
)

_PHI_MODEL_NAMES = {"Transcript", "Note"}
_AUDIT_CALL_NAMES = {"record_phi_access", "record_ingestion_event"}


def _reads_a_phi_model(source: str) -> bool:
    """True if the function's source fetches a PHI model — via
    `select(Transcript...)`/`select(Note...)` or `db.get(Transcript, ...)`
    /`db.get(Note, ...)` — as opposed to merely constructing one to write
    (`Transcript(...)`, a plain call with no `select`/`.get` wrapper).
    """
    return any(f"select({name}" in source or f".get({name}" in source for name in _PHI_MODEL_NAMES)


def _calls_an_audit_function(source: str) -> bool:
    return any(name in source for name in _AUDIT_CALL_NAMES)


def _function_sources(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    full_source = path.read_text(encoding="utf-8")
    sources: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            segment = ast.get_source_segment(full_source, node)
            if segment is not None:
                sources[node.name] = segment
    return sources


def test_phi_reading_service_functions_also_audit_the_read() -> None:
    offenders: list[str] = []

    for path in SERVICE_FILES:
        for name, source in _function_sources(path).items():
            if _reads_a_phi_model(source) and not _calls_an_audit_function(source):
                offenders.append(f"{path.relative_to(REPO_ROOT)}::{name}")

    assert offenders == [], (
        f"Function(s) read a PHI model without calling record_phi_access/"
        f"record_ingestion_event anywhere in their body: {offenders}"
    )


def test_guard_itself_is_meaningful() -> None:
    """Sanity check on the guard: if _reads_a_phi_model or
    _calls_an_audit_function ever stopped matching anything real (e.g.
    the functions were refactored away), the check above would silently
    pass having tested nothing.
    """
    matched_read = False
    matched_audit = False
    for path in SERVICE_FILES:
        for source in _function_sources(path).values():
            matched_read = matched_read or _reads_a_phi_model(source)
            matched_audit = matched_audit or _calls_an_audit_function(source)

    assert matched_read, "expected at least one function to read a PHI model"
    assert matched_audit, "expected at least one function to call an audit function"
