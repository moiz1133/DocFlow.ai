"""phase 8 purge completed audit action

Revision ID: f18c2302e066
Revises: e545adfa522b
Create Date: 2026-09-13 20:57:42.048328

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f18c2302e066"
down_revision: Union[str, None] = "e545adfa522b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Full audit_action value set immediately before this migration — needed
# by downgrade() to rebuild the enum without the Phase 8 addition. Same
# rename-swap-drop technique as a2dfb9c3f006/6243b19b618c/6e70ea786d86's
# downgrades (Postgres has no "ALTER TYPE ... DROP VALUE").
PRE_PHASE_8_AUDIT_ACTIONS = (
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
    "retention_skipped",
)


def upgrade() -> None:
    # Plain ADD VALUE inside a transaction is fine as long as the new
    # value isn't *used* in the same transaction, which this migration
    # doesn't do — same pattern as every prior audit_action addition.
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE 'purge_completed'"))


def downgrade() -> None:
    original_actions = ", ".join(f"'{a}'" for a in PRE_PHASE_8_AUDIT_ACTIONS)
    op.execute(sa.text("ALTER TYPE audit_action RENAME TO audit_action_old"))
    op.execute(sa.text(f"CREATE TYPE audit_action AS ENUM ({original_actions})"))
    op.execute(
        sa.text(
            "ALTER TABLE audit_logs ALTER COLUMN action TYPE audit_action "
            "USING action::text::audit_action"
        )
    )
    op.execute(sa.text("DROP TYPE audit_action_old"))
