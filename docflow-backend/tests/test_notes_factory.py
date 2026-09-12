"""Vendor-selection guardrails for app/notes/factory.py — mirrors
tests/test_transcription_factory.py exactly, one level down (notes
instead of transcription).
"""

import pytest

from app.config import Settings
from app.notes.factory import NoteGeneratorConfigurationError, build_note_generator


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_default_vendor_is_mock() -> None:
    generator = build_note_generator(_settings())
    assert generator.provider_name == "mock"


def test_synthetic_phi_mode_forces_mock_even_if_vendor_is_openai() -> None:
    generator = build_note_generator(
        _settings(PHI_MODE="synthetic", NOTE_GENERATOR_VENDOR="openai")
    )
    assert generator.provider_name == "mock"


def test_prod_with_mock_vendor_fails_fast() -> None:
    with pytest.raises(NoteGeneratorConfigurationError):
        build_note_generator(
            _settings(
                ENV="prod",
                DEBUG=False,
                PHI_MODE="real",
                NOTE_GENERATOR_VENDOR="mock",
                SECRET_KEY="a-real-looking-secret-key-value-1234",
                JWT_SECRET="a-real-looking-jwt-secret-value-1234",
            )
        )


def test_openai_vendor_without_api_key_fails_fast() -> None:
    with pytest.raises(NoteGeneratorConfigurationError):
        build_note_generator(_settings(PHI_MODE="real", NOTE_GENERATOR_VENDOR="openai"))


def test_openai_vendor_with_api_key_and_real_phi_mode_selects_openai() -> None:
    generator = build_note_generator(
        _settings(
            PHI_MODE="real",
            NOTE_GENERATOR_VENDOR="openai",
            OPENAI_API_KEY="sk-fake-test-key",
        )
    )
    assert generator.provider_name == "openai"
