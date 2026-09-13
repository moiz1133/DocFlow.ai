"""Envelope encryption for PHI-bearing columns (Phase 7).

Each value gets its own randomly generated 256-bit data-encryption key
(DEK), used once with AES-256-GCM (authenticated — tampering is detected
on decrypt, not silently accepted) to encrypt that one value. The DEK is
then "wrapped" (encrypted) by a KeyProvider's KEK and stored alongside
the ciphertext, self-describing: {key_version, wrapped_dek, nonce,
ciphertext}. This is what makes it "envelope" encryption — the KEK never
touches plaintext PHI directly, only ever wraps/unwraps per-value DEKs.

Blobs carry a fixed magic prefix (_MAGIC_PREFIX) so decrypt_value can
tell an already-migrated (encrypted) value apart from legacy plaintext
still awaiting the Phase 7 data migration — see decrypt_value's
docstring. This is the "legacy/unencrypted marker" tolerance the data
migration (and any mixed-data rollout window) relies on.

Used by app/security/encrypted_type.py's TypeDecorators and by
scripts/rotate_encryption_key.py; not meant to be called directly from
service/route code.
"""

import base64
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.security.keys import KeyProvider

_MAGIC_PREFIX = "phi-enc-v1:"
_DEK_BYTES = 32  # 256-bit data-encryption key
_NONCE_BYTES = 12  # 96-bit GCM nonce


class DecryptionError(Exception):
    """Raised when a blob's authentication tag doesn't verify (tampered
    or corrupted ciphertext) or the blob is otherwise malformed.
    """


def is_encrypted_blob(raw: str) -> bool:
    """Whether `raw` looks like one of our envelope blobs, as opposed to
    legacy plaintext left over from before the Phase 7 data migration.
    """
    return raw.startswith(_MAGIC_PREFIX)


def blob_key_version(raw: str) -> int | None:
    """The key_version a blob was encrypted under, or None if `raw` isn't
    one of our blobs (legacy plaintext). Used by tests/tooling that need
    to confirm a value's version without fully decrypting it.
    """
    if not is_encrypted_blob(raw):
        return None
    envelope = _parse_envelope(raw)
    version: int = envelope["key_version"]
    return version


def _parse_envelope(raw: str) -> dict[str, Any]:
    payload = raw[len(_MAGIC_PREFIX) :]
    envelope: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    return envelope


def encrypt_value(plaintext: str, *, key_provider: KeyProvider, key_version: int) -> str:
    dek = os.urandom(_DEK_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)
    wrapped_dek = key_provider.wrap_key(dek, key_version=key_version)

    envelope = {
        "key_version": key_version,
        "wrapped_dek": base64.b64encode(wrapped_dek).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
    return _MAGIC_PREFIX + encoded


def decrypt_value(raw: str, *, key_provider: KeyProvider) -> str:
    """Decrypts a blob produced by encrypt_value. If `raw` doesn't carry
    the envelope's magic prefix, it's treated as legacy, pre-Phase-7
    plaintext and returned unchanged — the tolerance that lets a mixed
    (partially migrated) dataset decrypt without crashing. Raises
    DecryptionError if it looks like an envelope but fails to
    authenticate (tampered/corrupted) or parse.
    """
    if not is_encrypted_blob(raw):
        return raw

    try:
        envelope = _parse_envelope(raw)
        key_version = envelope["key_version"]
        wrapped_dek = base64.b64decode(envelope["wrapped_dek"])
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
    except Exception as exc:
        raise DecryptionError(f"malformed encrypted blob: {exc}") from exc

    try:
        dek = key_provider.unwrap_key(wrapped_dek, key_version=key_version)
        plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError("ciphertext failed authentication (tampered or corrupted)") from exc
    except Exception as exc:
        raise DecryptionError(f"failed to decrypt value: {exc}") from exc

    return plaintext.decode("utf-8")
