"""The generated SOAP note for a session."""

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String
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

    # Phase 6 — generation provenance. Nullable: a future manually-authored
    # note (no generator involved) would legitimately have none of these.
    # See app/notes/prompts/soap_primary_care_v1.py for what prompt_version
    # tags, and app/services/note_service.py for degraded's meaning.
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Phase 7 — same retention gate as Transcript.is_retained: never
    # implied by the row merely existing. See
    # app/security/consent.py's ConsentService.assert_retention_allowed,
    # the single authority both Transcript and Note persistence defer to.
    is_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
