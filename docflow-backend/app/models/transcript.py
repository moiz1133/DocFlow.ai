"""The raw diarized transcript of a session. One-to-one with sessions."""

import uuid

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.db.types import PHIText


class Transcript(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "transcripts"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    content: Mapped[str] = mapped_column(PHIText, nullable=False)
    # Array of {speaker, start, end, text}. v1 (OpenAI) leaves speaker empty,
    # but the shape must already support diarization once that lands.
    segments: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Retention is gated by consent (see ConsentType.retention), not implied
    # by the transcript merely existing.
    is_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
