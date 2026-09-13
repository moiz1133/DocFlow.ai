"""SQLAlchemy TypeDecorators that transparently encrypt PHI on bind
(write) and decrypt on result (read) — see app/security/encryption.py
for the envelope scheme. app/db/types.py's PHIText subclasses
EncryptedText; this is the seam Phase 2 reserved PHIText for, so every
existing PHIText call site gets encryption with zero changes.

Deliberately NOT hooked for read-access auditing here (see
app/services/audit_service.py's module docstring): encryption/decryption
fires once per COLUMN per ROW, so hooking it would audit once per
decrypted field rather than once per logical operation — noisy, and not
what HIPAA access logging is for. Read auditing happens at the
service/route layer instead, once per operation.
"""

import json
from typing import Any

from sqlalchemy.types import Text, TypeDecorator

from app.config import get_settings
from app.security.encryption import decrypt_value, encrypt_value
from app.security.keys import get_key_provider


class EncryptedText(TypeDecorator[str]):
    """Encrypts a plain string column at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        settings = get_settings()
        return encrypt_value(
            value, key_provider=get_key_provider(), key_version=settings.ENCRYPTION_KEY_VERSION
        )

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt_value(value, key_provider=get_key_provider())


class EncryptedJSON(TypeDecorator[list[dict[str, object]]]):
    """Encrypts a JSON-serializable value by serializing it to text
    first, then applying the same envelope scheme as EncryptedText. Used
    for transcripts.segments, which must stay structured in Python but
    ends up encrypted-as-one-blob in the DB (see
    app/models/transcript.py) — Postgres can no longer validate it as
    JSONB once encrypted, which is expected: the column's SQL type is
    Text now, not JSONB (see the migration that changes it).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: list[dict[str, object]] | None, dialect: Any) -> str | None:
        if value is None:
            return None
        settings = get_settings()
        return encrypt_value(
            _dumps(value),
            key_provider=get_key_provider(),
            key_version=settings.ENCRYPTION_KEY_VERSION,
        )

    def process_result_value(
        self, value: str | None, dialect: Any
    ) -> list[dict[str, object]] | None:
        if value is None:
            return None
        decrypted = decrypt_value(value, key_provider=get_key_provider())
        return _loads(decrypted)


def _dumps(value: list[dict[str, object]]) -> str:
    return json.dumps(value)


def _loads(value: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = json.loads(value)
    return result
