"""The patient encounter ("session"). Named EncounterSession in Python to
avoid colliding with sqlalchemy.orm.Session / app.db.session — the table
itself is `sessions`.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.db.types import PHIText
from app.models.enums import SessionStatus


class EncounterSession(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "sessions"

    clinician_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", native_enum=True),
        nullable=False,
        server_default=SessionStatus.created.value,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # DATA MINIMIZATION: we never store raw audio, and we store as little
    # patient identity as possible — a single optional free-text field for
    # the clinician's own shorthand (e.g. "Rm 4, 2pm follow-up"), nothing
    # structured that would let this row alone identify a patient.
    patient_ref: Mapped[str | None] = mapped_column(PHIText, nullable=True)
