"""Immutable audit trail. No TimestampMixin: occurred_at is the one and
only timestamp an audit row ever has — there is no updated_at, because
there must never be an update.

Immutability is enforced twice:
  1. Here, at the app/ORM level, via a `before_update`/`before_delete`
     event that raises for any AuditLog instance.
  2. At the database level — see the initial migration and README, which
     REVOKE UPDATE, DELETE on audit_logs from the application's DB role.
Neither alone is sufficient: (1) is bypassed by anything that talks to
Postgres directly, and (2) is bypassed by anything running as the
admin/migration role. Both together make the log tamper-evident.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, event, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, UUIDPKMixin
from app.models.enums import AuditAction


class AuditLogImmutableError(Exception):
    """Raised when code attempts to modify or delete an audit log row."""


class AuditLog(Base, UUIDPKMixin, TenantMixin):
    __tablename__ = "audit_logs"

    # Nullable: some audit events are system-initiated, not user-initiated.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action", native_enum=True), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Mapped as `metadata_` because `metadata` is reserved on every
    # declarative class (Base.metadata); the actual DB column is still
    # named `metadata`. MUST stay PHI-free — note/transcript text must
    # never be written here, only structured, non-clinical context (e.g.
    # request id, field names touched).
    metadata_: Mapped[dict[str, object] | None] = mapped_column("metadata", JSONB, nullable=True)


@event.listens_for(AuditLog, "before_update")
def _block_audit_log_update(mapper: Mapper[AuditLog], connection: object, target: AuditLog) -> None:
    raise AuditLogImmutableError("audit_logs rows are immutable and cannot be updated")


@event.listens_for(AuditLog, "before_delete")
def _block_audit_log_delete(mapper: Mapper[AuditLog], connection: object, target: AuditLog) -> None:
    raise AuditLogImmutableError("audit_logs rows are immutable and cannot be deleted")
