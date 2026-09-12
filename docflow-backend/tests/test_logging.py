"""Tests for structured JSON logging and PHI redaction."""

import json
import logging

from app.logging import REDACTED, JSONFormatter, PHIRedactionFilter


def _build_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="handled encounter",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_transcript_field_is_redacted() -> None:
    record = _build_record(transcript="patient reports chest pain since Tuesday")

    filt = PHIRedactionFilter()
    filt.filter(record)

    formatter = JSONFormatter()
    payload = json.loads(formatter.format(record))

    assert payload["transcript"] == REDACTED
    assert "chest pain" not in json.dumps(payload)


def test_non_phi_fields_pass_through() -> None:
    record = _build_record(request_id="abc-123", env="dev")

    filt = PHIRedactionFilter()
    filt.filter(record)

    formatter = JSONFormatter()
    payload = json.loads(formatter.format(record))

    assert payload["request_id"] == "abc-123"
    assert payload["env"] == "dev"


def test_phi_field_is_case_insensitive() -> None:
    record = _build_record(SSN="123-45-6789")

    filt = PHIRedactionFilter()
    filt.filter(record)

    formatter = JSONFormatter()
    payload = json.loads(formatter.format(record))

    assert payload["SSN"] == REDACTED


def test_auth_secret_fields_are_redacted() -> None:
    """Phase 3: passwords, tokens, and MFA secrets must never reach logs,
    same as clinical PHI.
    """
    record = _build_record(
        password="hunter2",
        access_token="eyJhbGciOiJIUzI1NiJ9.fake.token",
        refresh_token="eyJhbGciOiJIUzI1NiJ9.fake.refresh",
        mfa_secret="JBSWY3DPEHPK3PXP",
        totp_code="123456",
    )

    filt = PHIRedactionFilter()
    filt.filter(record)

    formatter = JSONFormatter()
    payload = json.loads(formatter.format(record))

    assert payload["password"] == REDACTED
    assert payload["access_token"] == REDACTED
    assert payload["refresh_token"] == REDACTED
    assert payload["mfa_secret"] == REDACTED
    assert payload["totp_code"] == REDACTED
    assert "hunter2" not in json.dumps(payload)
