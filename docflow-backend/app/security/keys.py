"""Envelope-encryption key providers.

`KeyProvider` wraps/unwraps a per-value data-encryption key (DEK) using a
key-encryption key (KEK) it owns — the app never handles a KEK's raw
material outside of a provider. Selected by KEY_PROVIDER
("local" | "aws_kms", default "local"), same factory/guardrail shape as
app/transcription/factory.py's vendor selection.

LocalKeyProvider is dev/test only: static key(s) from Settings, no AWS.
AwsKmsProvider is the production path — it lazily imports boto3 (not a
hard dependency; only needed when KEY_PROVIDER=aws_kms) and is never
exercised against real AWS in CI or by the test suite.
"""

import base64
import os
from abc import ABC, abstractmethod
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings, get_settings

_NONCE_BYTES = 12  # 96-bit GCM nonce, the standard/recommended size
_DEK_KEY_BYTES = 32  # AES-256


class KeyProviderError(Exception):
    """Raised when a KEK is misconfigured, or a wrap/unwrap call fails."""


class KeyProvider(ABC):
    """Wraps/unwraps a plaintext data-encryption key using a KEK this
    provider owns. See app/security/encryption.py for how a DEK is
    actually used to encrypt a value.
    """

    @abstractmethod
    def wrap_key(self, plaintext_dek: bytes, *, key_version: int) -> bytes:
        """Encrypts a plaintext DEK for storage alongside a ciphertext blob."""
        raise NotImplementedError

    @abstractmethod
    def unwrap_key(self, wrapped_dek: bytes, *, key_version: int) -> bytes:
        """Decrypts a wrapped DEK back to its plaintext bytes."""
        raise NotImplementedError


class LocalKeyProvider(KeyProvider):
    """Dev/test KEK: one or more static 256-bit keys from env, keyed by
    version. No AWS, no network — this is what KEY_PROVIDER=local
    selects, and the only provider the test suite exercises.

    Wrapping is itself AES-256-GCM (the KEK encrypts the DEK, a random
    nonce prepended to the wrapped output) — a real, if simple, envelope
    scheme, not a no-op. Accepting multiple versions at once (rather than
    just "the current key") is what lets a rotation in progress decrypt
    both the old and new version — see scripts/rotate_encryption_key.py.
    """

    def __init__(self, keys: dict[int, bytes], *, active_version: int) -> None:
        if active_version not in keys:
            raise KeyProviderError(f"no local key configured for version {active_version}")
        for version, key in keys.items():
            if len(key) != _DEK_KEY_BYTES:
                raise KeyProviderError(
                    f"local encryption key for version {version} must be "
                    f"{_DEK_KEY_BYTES} bytes (AES-256), got {len(key)}"
                )
        self._keys = keys
        self.active_version = active_version

    def _key_for(self, key_version: int) -> bytes:
        try:
            return self._keys[key_version]
        except KeyError:
            raise KeyProviderError(f"no local key configured for version {key_version}") from None

    def wrap_key(self, plaintext_dek: bytes, *, key_version: int) -> bytes:
        kek = AESGCM(self._key_for(key_version))
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + kek.encrypt(nonce, plaintext_dek, None)

    def unwrap_key(self, wrapped_dek: bytes, *, key_version: int) -> bytes:
        kek = AESGCM(self._key_for(key_version))
        nonce, ciphertext = wrapped_dek[:_NONCE_BYTES], wrapped_dek[_NONCE_BYTES:]
        return kek.decrypt(nonce, ciphertext, None)


class AwsKmsProvider(KeyProvider):
    """Production KEK: AWS KMS. Each key_version maps to a distinct KMS
    key id — KMS's own automatic rotation is orthogonal to this (see
    README); in the common case there's exactly one entry, the current
    KMS_KEY_ID.

    boto3 is imported lazily in __init__, not at module scope: it's a
    real dependency that only needs to exist when this provider is
    actually selected, and it is not installed in the dev/test
    environment (KEY_PROVIDER=local there).
    """

    def __init__(self, key_ids: dict[int, str], *, active_version: int) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise KeyProviderError(
                "KEY_PROVIDER=aws_kms requires the boto3 package "
                "(pip install boto3) — not installed in this environment"
            ) from exc
        if active_version not in key_ids:
            raise KeyProviderError(f"no KMS key id configured for version {active_version}")
        self._key_ids = key_ids
        self.active_version = active_version
        self._client = boto3.client("kms")

    def _key_id_for(self, key_version: int) -> str:
        try:
            return self._key_ids[key_version]
        except KeyError:
            raise KeyProviderError(f"no KMS key id configured for version {key_version}") from None

    def wrap_key(self, plaintext_dek: bytes, *, key_version: int) -> bytes:
        response = self._client.encrypt(
            KeyId=self._key_id_for(key_version), Plaintext=plaintext_dek
        )
        blob: bytes = response["CiphertextBlob"]
        return blob

    def unwrap_key(self, wrapped_dek: bytes, *, key_version: int) -> bytes:
        # KMS's Decrypt identifies the key from the ciphertext blob
        # itself; passing KeyId here is an extra correctness check KMS
        # performs for us (it refuses if the blob wasn't encrypted under
        # that key).
        response = self._client.decrypt(
            CiphertextBlob=wrapped_dek, KeyId=self._key_id_for(key_version)
        )
        plaintext: bytes = response["Plaintext"]
        return plaintext


class KeyProviderConfigurationError(Exception):
    """Raised when KEY_PROVIDER is misconfigured — meant to fail
    application startup, not be caught and worked around at request time.
    """


def build_key_provider(settings: Settings) -> KeyProvider:
    if settings.ENV == "prod" and settings.KEY_PROVIDER == "local":
        raise KeyProviderConfigurationError(
            "KEY_PROVIDER=local with ENV=prod — a real KMS-backed provider "
            "is required in production."
        )

    if settings.KEY_PROVIDER == "local":
        if not settings.LOCAL_ENCRYPTION_KEY:
            raise KeyProviderConfigurationError(
                "KEY_PROVIDER=local requires LOCAL_ENCRYPTION_KEY to be set"
            )
        try:
            key_bytes = base64.b64decode(settings.LOCAL_ENCRYPTION_KEY, validate=True)
        except Exception as exc:
            raise KeyProviderConfigurationError(
                "LOCAL_ENCRYPTION_KEY must be base64-encoded 32 bytes"
            ) from exc
        return LocalKeyProvider(
            keys={settings.ENCRYPTION_KEY_VERSION: key_bytes},
            active_version=settings.ENCRYPTION_KEY_VERSION,
        )

    if settings.KEY_PROVIDER == "aws_kms":
        if not settings.KMS_KEY_ID:
            raise KeyProviderConfigurationError(
                "KEY_PROVIDER=aws_kms requires KMS_KEY_ID to be set"
            )
        return AwsKmsProvider(
            key_ids={settings.ENCRYPTION_KEY_VERSION: settings.KMS_KEY_ID},
            active_version=settings.ENCRYPTION_KEY_VERSION,
        )

    raise KeyProviderConfigurationError(f"Unknown KEY_PROVIDER: {settings.KEY_PROVIDER!r}")


@lru_cache
def get_key_provider() -> KeyProvider:
    return build_key_provider(get_settings())
