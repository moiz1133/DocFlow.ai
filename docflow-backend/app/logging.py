"""Structured JSON logging with PHI redaction.

PHI (protected health information) must never reach log output. All log
records are passed through PHIRedactionFilter, which redacts the value of
any field whose key name matches a known PHI-sensitive field, before the
JSON formatter renders the record.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

REDACTED = "[REDACTED]"

# Field names (case-insensitive) whose values must never appear in logs.
PHI_SENSITIVE_FIELDS = frozenset(
    {
        "audio",
        "transcript",
        "note",
        "soap",
        "subjective",
        "objective",
        "assessment",
        "plan",
        "patient",
        "patient_name",
        "dob",
        "ssn",
        "mrn",
        "email",
        "phone",
        "address",
        # Transcription content (Phase 4) — audio bytes and transcript
        # text must never reach logs, only metadata like provider name,
        # duration, or segment counts.
        "full_text",
        "segments",
        "audio_bytes",
        "audio_data",
        # Auth secrets — never PHI, but equally must never reach logs.
        "password",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "mfa_pending_token",
        "mfa_secret",
        "totp_code",
        "otp_code",
        "secret",
        "jwt_secret",
        "authorization",
    }
)

# Standard attributes present on every LogRecord; anything else attached to
# a record is treated as a caller-supplied "extra" field.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


def _redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (REDACTED if key.lower() in PHI_SENSITIVE_FIELDS else value)
        for key, value in mapping.items()
    }


class PHIRedactionFilter(logging.Filter):
    """Redacts PHI-sensitive fields from log record extras and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        for attr_name in list(vars(record).keys()):
            if attr_name in _STANDARD_RECORD_ATTRS:
                continue
            if attr_name.lower() in PHI_SENSITIVE_FIELDS:
                setattr(record, attr_name, REDACTED)

        if isinstance(record.args, dict):
            record.args = _redact_mapping(record.args)

        return True


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for attr_name, value in vars(record).items():
            if attr_name in _STANDARD_RECORD_ATTRS or attr_name in payload:
                continue
            payload[attr_name] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Configure the root logger with JSON output and PHI redaction.

    DEBUG-level logs are only emitted when settings.DEBUG is true; in
    production DEBUG is forced off (see Settings validator), so this also
    enforces that DEBUG logs never leak in prod.
    """
    root_logger = logging.getLogger()

    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(PHIRedactionFilter())

    effective_level = settings.LOG_LEVEL.upper()
    if effective_level == "DEBUG" and not settings.DEBUG:
        effective_level = "INFO"

    root_logger.addHandler(handler)
    root_logger.setLevel(effective_level)
