"""KeyProvider tests — LocalKeyProvider's wrap/unwrap round trip and the
build_key_provider factory guardrails (same shape as
tests/test_transcription_factory.py / tests/test_notes_factory.py).
"""

import base64

import pytest

from app.config import Settings, get_settings
from app.security.keys import (
    KeyProviderConfigurationError,
    KeyProviderError,
    LocalKeyProvider,
    build_key_provider,
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_wrap_then_unwrap_recovers_the_dek() -> None:
    provider = LocalKeyProvider(keys={1: b"k" * 32}, active_version=1)
    dek = b"d" * 32

    wrapped = provider.wrap_key(dek, key_version=1)
    assert wrapped != dek  # actually wrapped, not a no-op passthrough

    assert provider.unwrap_key(wrapped, key_version=1) == dek


def test_wrapping_the_same_dek_twice_produces_different_wrapped_output() -> None:
    """A fresh random nonce per wrap call — no determinism leak."""
    provider = LocalKeyProvider(keys={1: b"k" * 32}, active_version=1)
    dek = b"d" * 32

    assert provider.wrap_key(dek, key_version=1) != provider.wrap_key(dek, key_version=1)


def test_rejects_a_key_that_is_not_32_bytes() -> None:
    with pytest.raises(KeyProviderError):
        LocalKeyProvider(keys={1: b"too-short"}, active_version=1)


def test_rejects_an_active_version_with_no_matching_key() -> None:
    with pytest.raises(KeyProviderError):
        LocalKeyProvider(keys={1: b"k" * 32}, active_version=2)


def test_unwrap_with_an_unconfigured_version_raises() -> None:
    provider = LocalKeyProvider(keys={1: b"k" * 32}, active_version=1)
    wrapped = provider.wrap_key(b"d" * 32, key_version=1)

    with pytest.raises(KeyProviderError):
        provider.unwrap_key(wrapped, key_version=2)


def test_default_vendor_is_local() -> None:
    key = base64.b64encode(b"k" * 32).decode()
    provider = build_key_provider(_settings(LOCAL_ENCRYPTION_KEY=key))
    assert isinstance(provider, LocalKeyProvider)


def test_local_provider_requires_a_key() -> None:
    with pytest.raises(KeyProviderConfigurationError):
        build_key_provider(_settings(LOCAL_ENCRYPTION_KEY=None))


def test_local_provider_rejects_malformed_base64() -> None:
    with pytest.raises(KeyProviderConfigurationError):
        build_key_provider(_settings(LOCAL_ENCRYPTION_KEY="not valid base64!!"))


def test_prod_with_local_provider_fails_fast() -> None:
    key = base64.b64encode(b"k" * 32).decode()
    with pytest.raises(KeyProviderConfigurationError):
        build_key_provider(
            _settings(
                ENV="prod",
                DEBUG=False,
                PHI_MODE="real",
                SECRET_KEY="a-real-looking-secret-key-value-1234",
                JWT_SECRET="a-real-looking-jwt-secret-value-1234",
                KEY_PROVIDER="local",
                LOCAL_ENCRYPTION_KEY=key,
            )
        )


def test_aws_kms_without_key_id_fails_fast() -> None:
    with pytest.raises(KeyProviderConfigurationError):
        build_key_provider(_settings(KEY_PROVIDER="aws_kms", KMS_KEY_ID=None))


def test_get_key_provider_singleton_matches_test_session_settings() -> None:
    """Sanity check that the process-wide singleton (used by
    app/security/encrypted_type.py) actually resolves with this test
    session's LOCAL_ENCRYPTION_KEY — if this failed, every other
    encryption test's assumptions about a working local provider would
    be suspect too.
    """
    from app.security.keys import get_key_provider

    provider = get_key_provider()
    assert isinstance(provider, LocalKeyProvider)
    assert provider.active_version == get_settings().ENCRYPTION_KEY_VERSION
