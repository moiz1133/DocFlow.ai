"""audio ingestion stream_started audit action

Revision ID: 6243b19b618c
Revises: a2dfb9c3f006
Create Date: 2026-09-13 00:58:52.780917

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "6243b19b618c"
down_revision: Union[str, None] = "a2dfb9c3f006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The full value set immediately before this migration (initial schema's
# five CRUD actions plus Phase 3's auth events) — needed by downgrade() to
# rebuild audit_action without stream_started.
PRE_PHASE_5_AUDIT_ACTIONS = (
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
)


def upgrade() -> None:
    # Phase 5 (audio ingestion) needs a distinct audit event for "a client
    # opened a WS stream" — see app/models/enums.py's AuditAction and
    # app/services/transcription_service.py. Postgres allows a plain ADD
    # VALUE inside a transaction as long as the new value isn't used in
    # that same transaction, which this migration doesn't do.
    op.execute(sa.text("ALTER TYPE audit_action ADD VALUE 'stream_started'"))


def downgrade() -> None:
    # Same swap-the-type technique as a2dfb9c3f006's downgrade: Postgres
    # has no "ALTER TYPE ... DROP VALUE". Fails loudly if any audit_logs
    # row still uses 'stream_started' rather than silently corrupting it.
    original_list = ", ".join(f"'{a}'" for a in PRE_PHASE_5_AUDIT_ACTIONS)
    op.execute(sa.text("ALTER TYPE audit_action RENAME TO audit_action_old"))
    op.execute(sa.text(f"CREATE TYPE audit_action AS ENUM ({original_list})"))
    op.execute(
        sa.text(
            "ALTER TABLE audit_logs ALTER COLUMN action TYPE audit_action "
            "USING action::text::audit_action"
        )
    )
    op.execute(sa.text("DROP TYPE audit_action_old"))
