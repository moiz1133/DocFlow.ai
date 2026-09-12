"""The generated SOAP note for a session."""

import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.db.types import PHIText
from app.models.enums import NoteStatus


class Note(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "notes"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(50), nullable=False, server_default="soap")
    subjective: Mapped[str | None] = mapped_column(PHIText, nullable=True)
    objective: Mapped[str | None] = mapped_column(PHIText, nullable=True)
    assessment: Mapped[str | None] = mapped_column(PHIText, nullable=True)
    plan: Mapped[str | None] = mapped_column(PHIText, nullable=True)
    full_text: Mapped[str | None] = mapped_column(PHIText, nullable=True)
    status: Mapped[NoteStatus] = mapped_column(
        Enum(NoteStatus, name="note_status", native_enum=True),
        nullable=False,
        server_default=NoteStatus.draft.value,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
