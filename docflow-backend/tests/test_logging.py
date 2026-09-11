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
