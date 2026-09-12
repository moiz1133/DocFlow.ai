"""Patient/practice consent records. Session-level or practice-level."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import ConsentType


class Consent(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "consents"

    # Nullable: consent can be scoped to a single session or to the whole
    # practice (e.g. a standing training-data opt-out).
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    consent_type: Mapped[ConsentType] = mapped_column(
        Enum(ConsentType, name="consent_type", native_enum=True), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Must stay PHI-free: structured context only (e.g. consent method,
    # form version) — never patient identity or clinical text.
    meta: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
