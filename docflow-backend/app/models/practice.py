"""The tenant root. Every other PHI-bearing table hangs off practice_id."""

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPKMixin
from app.models.enums import PracticeStatus


class Practice(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "practices"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    specialty: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default="primary_care"
    )
    status: Mapped[PracticeStatus] = mapped_column(
        Enum(PracticeStatus, name="practice_status", native_enum=True),
        nullable=False,
        server_default=PracticeStatus.active.value,
    )
