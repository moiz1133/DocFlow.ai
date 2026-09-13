"""Unit tests for the envelope-encryption primitives — see
app/security/encryption.py and app/security/keys.py. Uses
LocalKeyProvider exclusively (KEY_PROVIDER=local is this whole test
suite's setting — see tests/conftest.py).
"""

import base64

import pytest

from app.security.encryption import (
    DecryptionError,
    blob_key_version,
    decrypt_value,
    encrypt_value,
    is_encrypted_blob,
)
from app.security.keys import LocalKeyProvider

_KEY_V1 = b"1" * 32
_KEY_V2 = b"2" * 32


def _provider(*, active_version: int = 1) -> LocalKeyProvider:
    return LocalKeyProvider(keys={1: _KEY_V1, 2: _KEY_V2}, active_version=active_version)


def test_round_trip_recovers_the_original_plaintext() -> None:
    provider = _provider()
    blob = encrypt_value("patient reports chest pain", key_provider=provider, key_version=1)

    assert decrypt_value(blob, key_provider=provider) == "patient reports chest pain"


def test_encrypted_blob_does_not_contain_the_plaintext() -> None:
    provider = _provider()
    plaintext = "a very specific and identifiable string ABCXYZ123"
    blob = encrypt_value(plaintext, key_provider=provider, key_version=1)

    assert plaintext not in blob
    assert is_encrypted_blob(blob)


def test_two_encryptions_of_the_same_plaintext_produce_different_blobs() -> None:
    """Each value gets its own random DEK and nonce — no ECB-like
    determinism that would let an observer spot repeated plaintexts.
    """
    provider = _provider()
    blob_a = encrypt_value("same text", key_provider=provider, key_version=1)
    blob_b = encrypt_value("same text", key_provider=provider, key_version=1)

    assert blob_a != blob_b
    assert decrypt_value(blob_a, key_provider=provider) == "same text"
    assert decrypt_value(blob_b, key_provider=provider) == "same text"


def test_blob_carries_its_key_version() -> None:
    provider = _provider()
    blob = encrypt_value("x", key_provider=provider, key_version=1)
    assert blob_key_version(blob) == 1

    blob_v2 = encrypt_value("x", key_provider=provider, key_version=2)
    assert blob_key_version(blob_v2) == 2


def test_legacy_plaintext_is_returned_unchanged_not_crashed_on() -> None:
    """The tolerance the Phase 7 data migration relies on: a value with
    no magic prefix is assumed to be legacy, pre-migration plaintext.
    """
    provider = _provider()
    legacy_plaintext = "this row was never encrypted"

    assert not is_encrypted_blob(legacy_plaintext)
    assert blob_key_version(legacy_plaintext) is None
    assert decrypt_value(legacy_plaintext, key_provider=provider) == legacy_plaintext


def test_tampered_ciphertext_fails_authentication() -> None:
    provider = _provider()
    blob = encrypt_value("patient reports chest pain", key_provider=provider, key_version=1)

    # Flip one bit deep inside the base64 payload (past the magic
    # prefix) — GCM's tag must fail to verify.
    tampered = blob[:-4] + ("A" if blob[-4] != "A" else "B") + blob[-3:]

    with pytest.raises(DecryptionError):
        decrypt_value(tampered, key_provider=provider)


def test_malformed_blob_raises_decryption_error_not_some_other_exception() -> None:
    provider = _provider()
    garbage = "phi-enc-v1:" + base64.urlsafe_b64encode(b"not valid json at all").decode()

    with pytest.raises(DecryptionError):
        decrypt_value(garbage, key_provider=provider)


def test_wrong_key_version_fails_to_decrypt() -> None:
    """A blob encrypted under a version the provider doesn't have any
    key for raises cleanly rather than silently returning garbage.
    """
    single_key_provider = LocalKeyProvider(keys={1: _KEY_V1}, active_version=1)
    two_key_provider = _provider()
    blob = encrypt_value("x", key_provider=two_key_provider, key_version=2)

    with pytest.raises(DecryptionError):
        decrypt_value(blob, key_provider=single_key_provider)


def test_both_key_versions_decrypt_during_a_rotation_transition() -> None:
    """A provider holding both the old and new key (as
    scripts/rotate_encryption_key.py's provider does mid-rotation) can
    decrypt values written under either version.
    """
    provider = _provider()  # holds both version 1 and version 2
    old_blob = encrypt_value("encrypted before rotation", key_provider=provider, key_version=1)
    new_blob = encrypt_value("encrypted after rotation", key_provider=provider, key_version=2)

    assert decrypt_value(old_blob, key_provider=provider) == "encrypted before rotation"
    assert decrypt_value(new_blob, key_provider=provider) == "encrypted after rotation"
