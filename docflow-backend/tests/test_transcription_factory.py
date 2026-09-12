"""Factory guardrails: vendor selection must fail fast, not fail weird."""

import pytest

from app.config import Settings
from app.transcription.factory import TranscriberConfigurationError, build_transcriber

BASE_KWARGS: dict[str, object] = {
    "SECRET_KEY": "a" * 40,
    "JWT_SECRET": "b" * 40,
    "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
    "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
    "REDIS_URL": "redis://localhost",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**BASE_KWARGS, **overrides})  # type: ignore[arg-type]


def test_default_vendor_is_mock() -> None:
    transcriber = build_transcriber(_settings())
    assert transcriber.provider_name == "mock"


def test_synthetic_phi_mode_forces_mock_even_if_vendor_is_openai() -> None:
    transcriber = build_transcriber(
        _settings(TRANSCRIBER_VENDOR="openai", PHI_MODE="synthetic", OPENAI_API_KEY="sk-fake")
    )
    assert transcriber.provider_name == "mock"


def test_prod_with_mock_vendor_fails_fast() -> None:
    with pytest.raises(TranscriberConfigurationError, match="prod"):
        build_transcriber(
            _settings(ENV="prod", DEBUG=False, PHI_MODE="real", TRANSCRIBER_VENDOR="mock")
        )


def test_openai_vendor_without_api_key_fails_fast() -> None:
    with pytest.raises(TranscriberConfigurationError, match="OPENAI_API_KEY"):
        build_transcriber(_settings(PHI_MODE="real", TRANSCRIBER_VENDOR="openai"))


def test_openai_vendor_with_api_key_and_real_phi_mode_selects_openai() -> None:
    transcriber = build_transcriber(
        _settings(PHI_MODE="real", TRANSCRIBER_VENDOR="openai", OPENAI_API_KEY="sk-fake")
    )
    assert transcriber.provider_name == "openai"
