"""Application settings, loaded from environment variables."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Substrings that flag an obviously-placeholder secret (matches the
# patterns used in .env.example). Not a strength check — just a guard
# against literally shipping the example value to prod.
_PLACEHOLDER_MARKERS = ("placeholder", "change-me", "changeme", "dev-only")


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return len(value) < 32 or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [origin.strip() for origin in value.split(",") if origin.strip()]
    return value


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

    # Distinct from SECRET_KEY on purpose: a JWT signing-key leak and a
    # general app-secret leak are different blast radii, and rotating one
    # must never force rotating the other.
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"

    # Session timeout has two halves: ACCESS_TOKEN_TTL_MINUTES bounds how
    # long a stolen *access* token is useful for; IDLE_TIMEOUT_MINUTES
    # bounds how long a *refresh* token stays useful when the client goes
    # quiet. REFRESH_TOKEN_TTL_DAYS is the hard outer ceiling regardless of
    # activity.
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 7
    IDLE_TIMEOUT_MINUTES: int = 30

    # Explicit origin lists, not "*": the extension origin
    # (chrome-extension://<id>) and web origins are both sent with the
    # Authorization header, and CORS forbids combining a wildcard origin
    # with allow_credentials=True anyway.
    CORS_WEB_ORIGINS: Annotated[list[str], NoDecode] = []
    CORS_EXTENSION_ORIGINS: Annotated[list[str], NoDecode] = []

    # Vendor selection for app/transcription/ — see
    # app/transcription/factory.py for the guardrails around this
    # (PHI_MODE=synthetic always forces "mock"; ENV=prod refuses "mock").
    TRANSCRIBER_VENDOR: Literal["mock", "openai"] = "mock"
    OPENAI_API_KEY: str | None = None
    # gpt-4o-transcribe is OpenAI's current general-purpose transcription
    # model; whisper-1 remains available where segment-level timestamps
    # matter more than transcription quality.
    OPENAI_TRANSCRIBE_MODEL: str = "gpt-4o-transcribe"
    TRANSCRIBE_TIMEOUT_SECONDS: float = 30.0
    TRANSCRIBE_MAX_RETRIES: int = 2

    @field_validator("CORS_WEB_ORIGINS", "CORS_EXTENSION_ORIGINS", mode="before")
    @classmethod
    def _parse_csv_origins(cls, value: object) -> object:
        return _split_csv(value)

    @model_validator(mode="after")
    def _validate_prod_debug(self) -> "Settings":
        if self.ENV == "prod" and self.DEBUG:
            raise ValueError("DEBUG must be False when ENV=prod")
        return self

    @model_validator(mode="after")
    def _validate_prod_secrets(self) -> "Settings":
        if self.ENV == "prod":
            if _looks_like_placeholder(self.SECRET_KEY):
                raise ValueError("SECRET_KEY must be a real secret when ENV=prod")
            if _looks_like_placeholder(self.JWT_SECRET):
                raise ValueError("JWT_SECRET must be a real secret when ENV=prod")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
