"""The raw diarized transcript of a session. One-to-one with sessions."""

import uuid

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.db.types import PHIText
from app.security.encrypted_type import EncryptedJSON


class Transcript(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "transcripts"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    content: Mapped[str] = mapped_column(PHIText, nullable=False)
    # Array of {speaker, start, end, text}. v1 (OpenAI) leaves speaker empty,
    # but the shape must already support diarization once that lands.
    # Encrypted as one serialized blob (Phase 7) — this column is no
    # longer real JSONB at the SQL level (see the migration that changed
    # it to Text), it just round-trips as list[dict] in Python. Never
    # queryable/filterable now — see README's "Encrypted columns" note.
    segments: Mapped[list[dict[str, object]]] = mapped_column(
        EncryptedJSON, nullable=False, server_default="[]"
    )
    # Retention is gated by consent (see ConsentType.retention), not implied
    # by the transcript merely existing.
    is_retained: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
