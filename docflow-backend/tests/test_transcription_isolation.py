"""The whole point of Phase 4 (and Phase 6's note generation, which
follows the identical rule): nothing outside a small, explicit allowlist
of files may import the `openai` SDK. Two independent checks:

1. Static (AST-based "grep"): no .py file under app/ (application
   source) other than the allowed importers below contains an
   `import openai` / `from openai import ...` statement. Scoped to app/
   deliberately: tests/ legitimately mocks the SDK to unit-test error
   translation (see test_transcription_openai.py, test_notes_openai.py)
   without ever making a real network call, and that's not the isolation
   this test guards.
2. Dynamic: importing the mock-vendor transcription path never pulls
   `openai` into sys.modules, proving the factory's lazy import (see
   app/transcription/factory.py) actually is lazy, not just documented
   as such. The equivalent check for note generation lives in
   tests/test_notes_isolation.py, alongside its own allowed-importer
   sanity check — this file keeps the one authoritative full-tree scan
   so both isolation guarantees aren't verified by two diverging copies
   of the same AST walk.

pyproject.toml's `flake8-tidy-imports` banned-api rule enforces the same
boundary at lint time (`make lint`) — this test is the runtime-verified
belt to that lint-time suspenders.
"""

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"
ALLOWED_OPENAI_IMPORTERS = {
    APP_ROOT / "transcription" / "openai.py",
    APP_ROOT / "notes" / "openai.py",
}


def _is_openai_name(name: str) -> bool:
    return name == "openai" or name.startswith("openai.")


def _imports_openai(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_openai_name(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module and _is_openai_name(node.module):
            return True
    return False


def test_no_app_module_outside_the_allowlist_imports_the_sdk() -> None:
    offenders: list[str] = []

    for py_file in APP_ROOT.rglob("*.py"):
        if py_file in ALLOWED_OPENAI_IMPORTERS:
            continue
        if "__pycache__" in py_file.parts:
            continue

        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        if _imports_openai(tree):
            offenders.append(str(py_file.relative_to(REPO_ROOT)))

    assert offenders == [], (
        f"Only {sorted(str(p.relative_to(REPO_ROOT)) for p in ALLOWED_OPENAI_IMPORTERS)} "
        f"may import the openai SDK; found imports in: {offenders}"
    )


def test_allowed_importers_actually_import_openai() -> None:
    """Sanity check on the test itself: if one of these ever stops being
    true (a file gets renamed, the import removed), the isolation test
    above would silently stop testing anything meaningful for it.
    """
    for allowed_file in ALLOWED_OPENAI_IMPORTERS:
        tree = ast.parse(allowed_file.read_text(encoding="utf-8"))
        assert _imports_openai(tree), f"{allowed_file} was expected to import openai but doesn't"


def test_selecting_mock_vendor_never_imports_the_openai_sdk() -> None:
    sys.modules.pop("openai", None)

    from app.config import Settings
    from app.transcription.factory import build_transcriber

    settings = Settings(
        SECRET_KEY="a" * 40,
        JWT_SECRET="b" * 40,
        DATABASE_URL="postgresql+asyncpg://a:b@localhost/x",
        APP_DATABASE_URL="postgresql+asyncpg://a:b@localhost/x",
        REDIS_URL="redis://localhost",
        TRANSCRIBER_VENDOR="mock",
    )
    transcriber = build_transcriber(settings)
    assert transcriber.provider_name == "mock"
    assert "openai" not in sys.modules
