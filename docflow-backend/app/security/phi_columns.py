"""The canonical list of (table, column) pairs backed by PHIText or
EncryptedJSON — every PHI column in the schema as of Phase 7. Single
source of truth shared by the Phase 7 data migration
(alembic/versions/e545adfa522b_*.py) and scripts/rotate_encryption_key.py
so the two never drift apart.

Deliberately excludes users.email (workforce login data, kept plaintext
so it stays indexable/queryable — see README's "Encrypted columns"
section for that tradeoff).
"""

PHI_COLUMNS: tuple[tuple[str, str], ...] = (
    ("transcripts", "content"),
    ("transcripts", "segments"),
    ("notes", "subjective"),
    ("notes", "objective"),
    ("notes", "assessment"),
    ("notes", "plan"),
    ("notes", "full_text"),
    ("sessions", "patient_ref"),
    ("users", "mfa_secret"),
)
