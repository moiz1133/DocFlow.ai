"""Clinicians and staff."""

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.db.types import PHIText
from app.models.enums import UserRole


class User(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Set by POST /v1/auth/mfa/setup, only takes effect (mfa_enabled=True)
    # once confirmed by POST /v1/auth/mfa/verify. PHIText-typed: not
    # clinical PHI, but it's a secret that deserves the same "isolated
    # behind a swappable column type" treatment for Phase 7 encryption.
    mfa_secret: Mapped[str | None] = mapped_column(PHIText, nullable=True)
