"""Application settings, loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    ENV: Literal["dev", "staging", "prod"] = "dev"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    SECRET_KEY: str

    # Admin/migration DSN: owns the schema, runs Alembic. Must NOT be used
    # for application queries — it is typically a superuser role in local
    # dev (the Postgres image's bootstrap user), which bypasses Row-Level
    # Security entirely.
    DATABASE_URL: str

    # Application runtime DSN: the restricted, non-superuser, NOBYPASSRLS
    # role that all request-scoped queries must use so tenant-isolation
    # policies are actually enforced. See README for the role requirements.
    APP_DATABASE_URL: str

    REDIS_URL: str

    # "synthetic": safe for dev/test with fake data. "real": requires a BAA'd
    # vendor stack; later phases must enforce that prod never runs "synthetic"
    # and that "real" mode has verified vendor agreements in place.
    PHI_MODE: Literal["synthetic", "real"] = "synthetic"

    @model_validator(mode="after")
    def _validate_prod_debug(self) -> "Settings":
        if self.ENV == "prod" and self.DEBUG:
            raise ValueError("DEBUG must be False when ENV=prod")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
