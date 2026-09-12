"""soap note generation columns and enum values

Revision ID: 6e70ea786d86
Revises: 6243b19b618c
Create Date: 2026-09-13 01:44:39.079115

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "6e70ea786d86"
down_revision: Union[str, None] = "6243b19b618c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Full value sets immediately before this migration — needed by
# downgrade() to rebuild both enum types without the Phase 6 additions.
PRE_PHASE_6_AUDIT_ACTIONS = (
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
)
PRE_PHASE_6_SESSION_STATUSES = (
    "created",
    "recording",
    "transcribing",
    "generating",
    "complete",
    "error",
)


def upgrade() -> None:
    # --- Note generation provenance (Phase 6) --------------------------
    op.add_column("notes", sa.Column("prompt_version", sa.String(length=100), nullable=True))
    op.add_column("notes", sa.Column("provider", sa.String(length=50), nullable=True))
    op.add_column("notes", sa.Column("model", sa.String(length=100), nullable=True))
    op.add_column(
        "notes",
        sa.Column("degraded", sa.Boolean(), server_default="false", nullable=False),
    )

    # --- New enum values -------------------------------------------------
    # Postgres allows a plain ADD VALUE inside a transaction as long as
    # the new value isn't *used* in that same transaction, which this
    # migration doesn't do (see a2dfb9c3f006 / 6243b19b618c for the same
    # pattern applied to audit_action previously).
    op.execute(sa.text("ALTER TYPE session_status ADD VALUE 'complete_degraded'"))
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE 'note_degraded'"))


def downgrade() -> None:
    op.drop_column("notes", "degraded")
    op.drop_column("notes", "model")
    op.drop_column("notes", "provider")
    op.drop_column("notes", "prompt_version")

    # Same swap-the-type technique as a2dfb9c3f006 / 6243b19b618c's
    # downgrades: Postgres has no "ALTER TYPE ... DROP VALUE". Fails
    # loudly if any row still uses a value being removed, rather than
    # silently corrupting that data.
    original_actions = ", ".join(f"'{a}'" for a in PRE_PHASE_6_AUDIT_ACTIONS)
    op.execute(sa.text("ALTER TYPE audit_action RENAME TO audit_action_old"))
    op.execute(sa.text(f"CREATE TYPE audit_action AS ENUM ({original_actions})"))
    op.execute(
        sa.text(
            "ALTER TABLE audit_logs ALTER COLUMN action TYPE audit_action "
            "USING action::text::audit_action"
        )
    )
    op.execute(sa.text("DROP TYPE audit_action_old"))

    # sessions.status has a server_default (unlike audit_logs.action,
    # which has none) — explicitly drop and re-add it around the type
    # swap rather than assume Postgres carries a default referencing the
    # old type's OID forward onto the new one.
    original_statuses = ", ".join(f"'{s}'" for s in PRE_PHASE_6_SESSION_STATUSES)
    op.execute(sa.text("ALTER TABLE sessions ALTER COLUMN status DROP DEFAULT"))
    op.execute(sa.text("ALTER TYPE session_status RENAME TO session_status_old"))
    op.execute(sa.text(f"CREATE TYPE session_status AS ENUM ({original_statuses})"))
    op.execute(
        sa.text(
            "ALTER TABLE sessions ALTER COLUMN status TYPE session_status "
            "USING status::text::session_status"
        )
    )
    op.execute(
        sa.text("ALTER TABLE sessions ALTER COLUMN status SET DEFAULT 'created'::session_status")
    )
    op.execute(sa.text("DROP TYPE session_status_old"))
