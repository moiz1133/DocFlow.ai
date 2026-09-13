"""run_startup_safety_checks — the Phase 7 fail-fast PHI_MODE self-check.
See app/security/startup_checks.py.
"""

import base64
import logging

import pytest

from app.config import Settings
from app.security.startup_checks import StartupSafetyCheckFailed, run_startup_safety_checks

_LOCAL_KEY = base64.b64encode(b"k" * 32).decode()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
        "LOCAL_ENCRYPTION_KEY": _LOCAL_KEY,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def _valid_prod_like_settings(**overrides: object) -> Settings:
    """A configuration that should pass every Phase 7 startup check —
    used as the "flip one thing at a time" baseline below.
    """
    base: dict[str, object] = {
        "ENV": "prod",
        "DEBUG": False,
        "PHI_MODE": "real",
        "SECRET_KEY": "a-real-looking-secret-key-value-1234",
        "JWT_SECRET": "a-real-looking-jwt-secret-value-1234",
        "KEY_PROVIDER": "aws_kms",
        "KMS_KEY_ID": "arn:aws:kms:us-east-1:111111111111:key/fake",
        "TRANSCRIBER_VENDOR": "openai",
        "NOTE_GENERATOR_VENDOR": "openai",
        "OPENAI_API_KEY": "sk-fake-test-key",
        "ENFORCE_TLS": True,
        "TRUST_PROXY_HEADERS": True,
        "TRANSCRIPT_RETENTION_DEFAULT": "consented",
        "ALLOW_BLANKET_RETENTION": False,
    }
    return _settings(**{**base, **overrides})


def test_prod_with_synthetic_phi_mode_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_settings(ENV="prod", DEBUG=False, PHI_MODE="synthetic"))


def test_prod_with_debug_refuses_to_start() -> None:
    # Built as dev first (Settings' own _validate_prod_debug validator
    # would already refuse ENV=prod + DEBUG=true at construction time),
    # then mutated directly — Settings has no validate_assignment, so
    # this bypasses that validator and exercises the startup check's own
    # independent (defense-in-depth) enforcement of the same rule.
    settings = _settings(ENV="dev", DEBUG=True, PHI_MODE="synthetic")
    settings.ENV = "prod"
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(settings)


def test_real_phi_mode_with_local_key_provider_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_valid_prod_like_settings(KEY_PROVIDER="local", KMS_KEY_ID=None))


def test_real_phi_mode_with_mock_transcriber_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_valid_prod_like_settings(TRANSCRIBER_VENDOR="mock"))


def test_real_phi_mode_with_mock_note_generator_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_valid_prod_like_settings(NOTE_GENERATOR_VENDOR="mock"))


def test_real_phi_mode_with_openai_vendor_but_no_api_key_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_valid_prod_like_settings(OPENAI_API_KEY=None))


def test_real_phi_mode_with_tls_disabled_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_valid_prod_like_settings(ENFORCE_TLS=False))


def test_real_phi_mode_prod_without_trust_proxy_headers_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(_valid_prod_like_settings(TRUST_PROXY_HEADERS=False))


def test_real_phi_mode_with_blanket_retention_and_no_opt_in_refuses_to_start() -> None:
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(
            _valid_prod_like_settings(
                TRANSCRIPT_RETENTION_DEFAULT="always", ALLOW_BLANKET_RETENTION=False
            )
        )


def test_note_tracing_enabled_with_cloud_langfuse_host_refuses_to_start() -> None:
    # Settings itself already refuses to construct with this combination
    # (_validate_tracing_is_self_hosted) — bypass that the same way
    # test_prod_with_debug_refuses_to_start bypasses _validate_prod_debug,
    # to exercise the startup check's own independent re-assertion.
    settings = _valid_prod_like_settings(NOTE_TRACING_ENABLED=False, LANGFUSE_HOST=None)
    settings.NOTE_TRACING_ENABLED = True
    settings.LANGFUSE_HOST = "https://cloud.langfuse.com"
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(settings)


def test_note_tracing_enabled_with_no_langfuse_host_refuses_to_start() -> None:
    settings = _valid_prod_like_settings(NOTE_TRACING_ENABLED=False, LANGFUSE_HOST=None)
    settings.NOTE_TRACING_ENABLED = True
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(settings)


def test_note_tracing_enabled_with_self_hosted_langfuse_host_passes() -> None:
    run_startup_safety_checks(
        _valid_prod_like_settings(
            NOTE_TRACING_ENABLED=True, LANGFUSE_HOST="https://langfuse.internal.example.com"
        )
    )


def test_trace_include_content_under_phi_mode_real_refuses_to_start() -> None:
    # Settings itself already refuses this combination too
    # (_validate_trace_content_restricted) — same bypass-then-reassert
    # pattern as the tracing tests above.
    settings = _valid_prod_like_settings(TRACE_INCLUDE_CONTENT=False)
    settings.TRACE_INCLUDE_CONTENT = True
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(settings)


def test_trace_include_content_under_env_prod_refuses_to_start() -> None:
    settings = _settings(ENV="dev", DEBUG=False, PHI_MODE="synthetic", TRACE_INCLUDE_CONTENT=False)
    settings.ENV = "prod"
    settings.TRACE_INCLUDE_CONTENT = True
    with pytest.raises(StartupSafetyCheckFailed):
        run_startup_safety_checks(settings)


def test_real_phi_mode_with_blanket_retention_and_opt_in_passes() -> None:
    run_startup_safety_checks(
        _valid_prod_like_settings(
            TRANSCRIPT_RETENTION_DEFAULT="always", ALLOW_BLANKET_RETENTION=True
        )
    )


def test_valid_prod_like_configuration_passes() -> None:
    run_startup_safety_checks(_valid_prod_like_settings())


def test_dev_synthetic_configuration_passes() -> None:
    run_startup_safety_checks(_settings(ENV="dev", DEBUG=True, PHI_MODE="synthetic"))


def test_engaged_controls_banner_is_logged_on_success(caplog: pytest.LogCaptureFixture) -> None:
    # alembic/env.py's fileConfig(...) call — run once per test session by
    # the _provisioned_database fixture (see tests/conftest.py), before
    # this test's own logger has ever emitted anything — disables every
    # pre-existing logger not explicitly listed in alembic.ini, including
    # this one (created at module-import time during pytest collection).
    # Harmless in production (that only ever runs once, at process start,
    # before app logging is configured — see app/main.py's lifespan
    # calling configure_logging() first) but it means this test has to
    # explicitly re-enable the logger to observe it via caplog.
    logging.getLogger("app.security.startup_checks").disabled = False

    with caplog.at_level(logging.INFO, logger="app.security.startup_checks"):
        run_startup_safety_checks(_valid_prod_like_settings())

    banner_records = [r for r in caplog.records if r.message == "HIPAA controls engaged"]
    assert len(banner_records) == 1
    banner = banner_records[0]
    assert banner.encryption_key_provider == "aws_kms"  # type: ignore[attr-defined]
    assert banner.tls_enforced is True  # type: ignore[attr-defined]
    # PHI-free: no secret/key material anywhere in the record's own dict
    # — only which provider/vendor is selected, never the key/API key
    # values themselves.
    rendered = str(banner.__dict__)
    assert "fake-test-key" not in rendered
    assert "arn:aws:kms" not in rendered
