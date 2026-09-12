"""Notes-specific half of the shared isolation guarantee — see
tests/test_transcription_isolation.py for the authoritative full-tree
static scan (it covers app/notes/ too) and the allowed-importer sanity
check. This file only adds the dynamic check specific to note
generation: selecting the mock vendor never touches the openai SDK.
"""

import sys


def test_selecting_mock_vendor_never_imports_the_openai_sdk() -> None:
    sys.modules.pop("openai", None)

    from app.config import Settings
    from app.notes.factory import build_note_generator

    settings = Settings(
        SECRET_KEY="a" * 40,
        JWT_SECRET="b" * 40,
        DATABASE_URL="postgresql+asyncpg://a:b@localhost/x",
        APP_DATABASE_URL="postgresql+asyncpg://a:b@localhost/x",
        REDIS_URL="redis://localhost",
        NOTE_GENERATOR_VENDOR="mock",
    )
    generator = build_note_generator(settings)
    assert generator.provider_name == "mock"
    assert "openai" not in sys.modules
