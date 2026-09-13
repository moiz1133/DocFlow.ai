"""PHI-bearing column type.

Every column that can hold protected health information (transcript text,
SOAP note text, a clinician's free-text patient reference, etc.) uses
`PHIText` instead of raw `Text`. Phase 2 reserved this as a plain
pass-through `Text` alias specifically so Phase 7 could swap in an
encrypting `TypeDecorator` for every PHI column at once, without
touching call sites or running a schema-churning migration for each
affected table — that swap has now happened: `PHIText` is
`EncryptedText` (see app/security/encrypted_type.py and
app/security/encryption.py for the envelope-encryption scheme).
"""

from app.security.encrypted_type import EncryptedText


class PHIText(EncryptedText):
    """PHI-bearing text column — encrypted at rest (AES-256-GCM envelope
    encryption, key-wrapped via app/security/keys.py's KeyProvider).
    """
