"""hipaa controls: field encryption, note retention, audit action

Revision ID: e545adfa522b
Revises: 6e70ea786d86
Create Date: 2026-09-13 14:51:10.768086

Three things happen here:

1. Schema: notes.is_retained (mirrors Transcript.is_retained — see
   app/models/note.py), transcripts.segments changes from JSONB to Text
   (it's encrypted-as-one-blob now — see app/security/encrypted_type.py's
   EncryptedJSON — so Postgres can no longer validate it as JSON), and a
   new audit_action value (retention_skipped).
2. Data: every existing plaintext value in a PHIText/EncryptedJSON column
   gets encrypted in place, using whatever KeyProvider the environment
   running this migration is configured with (KEY_PROVIDER/
   LOCAL_ENCRYPTION_KEY or KMS_KEY_ID — see app/security/keys.py).
3. Tolerance: _encrypt_column skips any value that already looks like one
   of our envelope blobs (app.security.encryption.is_encrypted_blob), so
   this migration is safe to run against a database with a mix of
   already-migrated and legacy-plaintext rows, and safe to re-run.

Dev/test databases are synthetic and typically empty at migration time
(see tests/conftest.py — the throwaway `docflow_test` database starts
empty, migrates to head, then tests create rows through the app, which
now encrypts on write automatically) — reseeding is equally valid there;
this data-migration step exists for anywhere that already has real rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.security.encryption import decrypt_value, encrypt_value, is_encrypted_blob
from app.security.keys import build_key_provider
from app.security.phi_columns import PHI_COLUMNS

# revision identifiers, used by Alembic.
revision: str = "e545adfa522b"
down_revision: Union[str, None] = "6e70ea786d86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRE_PHASE_7_AUDIT_ACTIONS = (
    "create",
    "read",
    "update",
    "delete",
    "export",
    "register",
    "login_success",
    "login_failure",
    "logout",
    "mfa_enrolled",
    "mfa_verified",
    "token_reuse_detected",
    "stream_started",
    "note_degraded",
)


def _encrypt_column(connection: Connection, *, table: str, column: str) -> None:
    # f-string interpolation of `table`/`column` is safe here: both only
    # ever come from this module's own hardcoded PHI_COLUMNS tuple,
    # never from user input — the values (bound as params below) are the
    # untrusted part, and those go through proper bound parameters.
    settings = get_settings()
    key_provider = build_key_provider(settings)

    rows = connection.execute(
        sa.text(f'SELECT id, "{column}" FROM {table} WHERE "{column}" IS NOT NULL')
    ).fetchall()
    for row_id, value in rows:
        if not value or is_encrypted_blob(value):
            continue
        encrypted = encrypt_value(
            value, key_provider=key_provider, key_version=settings.ENCRYPTION_KEY_VERSION
        )
        connection.execute(
            sa.text(f'UPDATE {table} SET "{column}" = :val WHERE id = :row_id'),
            {"val": encrypted, "row_id": row_id},
        )


def _decrypt_column(connection: Connection, *, table: str, column: str) -> None:
    settings = get_settings()
    key_provider = build_key_provider(settings)

    rows = connection.execute(
        sa.text(f'SELECT id, "{column}" FROM {table} WHERE "{column}" IS NOT NULL')
    ).fetchall()
    for row_id, value in rows:
        if not value or not is_encrypted_blob(value):
            continue
        plaintext = decrypt_value(value, key_provider=key_provider)
        connection.execute(
            sa.text(f'UPDATE {table} SET "{column}" = :val WHERE id = :row_id'),
            {"val": plaintext, "row_id": row_id},
        )


def upgrade() -> None:
    op.add_column(
        "notes", sa.Column("is_retained", sa.Boolean(), server_default="false", nullable=False)
    )

    # transcripts.segments: JSONB -> Text. `USING segments::text` casts
    # each existing JSONB value to its text representation, which is
    # still plain (unencrypted) JSON at this point — _encrypt_column
    # below then encrypts it like any other PHI column.
    op.alter_column(
        "transcripts",
        "segments",
        type_=sa.Text(),
        server_default="[]",
        postgresql_using="segments::text",
    )

    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE 'retention_skipped'"))

    connection = op.get_bind()
    for table, column in PHI_COLUMNS:
        _encrypt_column(connection, table=table, column=column)


def downgrade() -> None:
    connection = op.get_bind()
    for table, column in PHI_COLUMNS:
        _decrypt_column(connection, table=table, column=column)

    original_actions = ", ".join(f"'{a}'" for a in PRE_PHASE_7_AUDIT_ACTIONS)
    op.execute(sa.text("ALTER TYPE audit_action RENAME TO audit_action_old"))
    op.execute(sa.text(f"CREATE TYPE audit_action AS ENUM ({original_actions})"))
    op.execute(
        sa.text(
            "ALTER TABLE audit_logs ALTER COLUMN action TYPE audit_action "
            "USING action::text::audit_action"
        )
    )
    op.execute(sa.text("DROP TYPE audit_action_old"))

    # Same "drop default, change type, re-add default" sequence as
    # 6e70ea786d86's session_status downgrade: Postgres won't
    # automatically re-cast an existing text default to jsonb.
    op.execute(sa.text("ALTER TABLE transcripts ALTER COLUMN segments DROP DEFAULT"))
    op.alter_column(
        "transcripts",
        "segments",
        type_=postgresql.JSONB(astext_type=sa.Text()),
        postgresql_using="segments::jsonb",
    )
    op.execute(sa.text("ALTER TABLE transcripts ALTER COLUMN segments SET DEFAULT '[]'::jsonb"))

    op.drop_column("notes", "is_retained")
