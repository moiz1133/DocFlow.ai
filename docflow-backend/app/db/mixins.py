"""Reusable declarative mixins shared by every domain model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPKMixin:
    """Adds a UUID primary key, generated client-side and mirrored server-side."""

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class TimestampMixin:
    """Adds created_at/updated_at, both stamped by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Adds the practice_id column that scopes a row to a tenant.

    Every PHI-bearing table carries this column, indexed, and it is the
    column Row-Level Security policies key off of (see the initial
    migration). It is never nullable: a row with no tenant cannot exist.
    """

    practice_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("practices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
