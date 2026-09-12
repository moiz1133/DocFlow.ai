"""Refresh-token records backing rotation and reuse detection.

Only the token's identifier (jti) and bookkeeping live here — never the
token itself. A refresh JWT's signature is enough to prove authenticity;
this table exists purely to know whether a given jti has already been
rotated (reuse detection) or gone idle too long (IDLE_TIMEOUT_MINUTES).

No TimestampMixin: like audit_logs, this row's lifecycle is tracked by its
own domain-specific timestamps (created_at, last_activity_at) rather than
a generic updated_at.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, UUIDPKMixin


class RefreshToken(Base, UUIDPKMixin, TenantMixin):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Bumped on every successful refresh; a rotation request against a
    # token whose last_activity_at is older than IDLE_TIMEOUT_MINUTES is
    # refused even though the token itself hasn't expired yet.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
