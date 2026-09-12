"""Clinicians and staff. Auth fields are defined but unused until Phase 3."""

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TenantMixin, TimestampMixin, UUIDPKMixin
from app.models.enums import UserRole


class User(Base, UUIDPKMixin, TenantMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # Nullable: no auth logic exists yet (Phase 3). The column is defined
    # now so the schema doesn't churn when password auth lands.
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
