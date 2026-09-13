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

    # Hard ceilings for a single WS audio-streaming session (see
    # app/api/sessions.py) — exceeding either ends the connection with
    # close code 4413 rather than letting a runaway client stream forever.
    MAX_SESSION_SECONDS: int = 3600
    MAX_AUDIO_BYTES: int = 100 * 1024 * 1024
    # Bounded per-connection audio queue depth. A client producing audio
    # faster than the transcriber can consume it blocks on the queue
    # (backpressure) instead of buffering without limit — see
    # app/services/transcription_service.py.
    WS_BUFFER_MAX_CHUNKS: int = 50

    # Governs Transcript.is_retained when no session-scoped retention
    # Consent (ConsentType.retention) exists — see
    # app/services/transcription_service.py's _resolve_retention.
    # "none": never retain, regardless of consent. "consented" (default):
    # retain only with an explicit granted retention consent on file.
    # "always": retain unconditionally.
    TRANSCRIPT_RETENTION_DEFAULT: Literal["none", "consented", "always"] = "consented"

    # Vendor selection for app/notes/ — see app/notes/factory.py for the
    # guardrails (same shape as TRANSCRIBER_VENDOR's: PHI_MODE=synthetic
    # always forces "mock"; ENV=prod refuses "mock"). OPENAI_API_KEY is
    # shared with the transcription vendor; the note-generation model is
    # deliberately separate since the two calls have different needs.
    NOTE_GENERATOR_VENDOR: Literal["mock", "openai"] = "mock"
    OPENAI_NOTE_MODEL: str = "gpt-4o"
    NOTE_MAX_RETRIES: int = 2
    NOTE_TIMEOUT_SECONDS: float = 30.0
    # Conservative heuristic pre-step (app/notes/scrub.py) that strips
    # non-clinical chatter before the transcript reaches the note
    # generator. Toggleable because it's a judgment call some deployments
    # may want to disable while tuning it.
    NOTE_SCRUB_ENABLED: bool = True
    # Tag persisted on every generated Note (see app/models/note.py) so a
    # prompt change is traceable to exactly which notes used it — bump
    # this (and add a new app/notes/prompts/soap_primary_care_vN.py) when
    # the prompt changes, rather than editing the existing version in place.
    NOTE_PROMPT_VERSION: str = "soap_primary_care_v1"

    # LLM tracing for app/notes/ generation calls — off by default. When
    # enabled, LANGFUSE_HOST must point at a SELF-HOSTED Langfuse
    # instance: Langfuse Cloud would mean transcript/note PHI leaving this
    # process to a third party, which is never acceptable here regardless
    # of BAA status for the LLM vendor itself.
    NOTE_TRACING_ENABLED: bool = False
    LANGFUSE_HOST: str | None = None

    # --- Phase 7: HIPAA controls --------------------------------------

    # Field-level PHI encryption — see app/security/keys.py and
    # app/security/encryption.py. "local" (default) needs no AWS and is
    # the only provider the test suite exercises; "aws_kms" is the
    # production path (refused with ENV=prod + KEY_PROVIDER=local, same
    # guardrail shape as the vendor factories).
    KEY_PROVIDER: Literal["local", "aws_kms"] = "local"
    # Base64-encoded 32-byte AES-256 key. Dev/test only — deliberately no
    # default value here (unlike e.g. NOTE_PROMPT_VERSION): a shared
    # hardcoded key would defeat the point. Required when KEY_PROVIDER
    # stays "local" (its default), same as SECRET_KEY/JWT_SECRET.
    LOCAL_ENCRYPTION_KEY: str | None = None
    KMS_KEY_ID: str | None = None
    # Bumped when rotating keys — see scripts/rotate_encryption_key.py
    # and README's "Key rotation" section. Stamped into every new
    # encrypted blob; existing blobs keep decrypting under whatever
    # version they were written with.
    ENCRYPTION_KEY_VERSION: int = 1

    # TLS enforcement (app/security/transport.py). Default True: a
    # deployer has to opt OUT for local dev (.env.example does), not opt
    # in for prod. TRUST_PROXY_HEADERS controls whether X-Forwarded-Proto
    # from a terminating load balancer is trusted to mean "this request
    # arrived over HTTPS" — default False (don't trust a header that's
    # trivially spoofable unless a deployment explicitly confirms it sits
    # behind a proxy that sets it correctly and strips any client-supplied
    # copy).
    ENFORCE_TLS: bool = True
    TRUST_PROXY_HEADERS: bool = False

    # Consent gate (app/security/consent.py). TRANSCRIPT_RETENTION_DEFAULT
    # ="always" retains unconditionally for every session/practice with no
    # per-session consent check at all — a real compliance risk (blanket
    # retention without a signed agreement backing it) — so it additionally
    # requires this explicit opt-in, checked both here at request time
    # (ConsentService) and at startup (app/security/startup_checks.py).
    ALLOW_BLANKET_RETENTION: bool = False

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

    @model_validator(mode="after")
    def _validate_tracing_is_self_hosted(self) -> "Settings":
        if self.NOTE_TRACING_ENABLED:
            if not self.LANGFUSE_HOST:
                raise ValueError("LANGFUSE_HOST must be set when NOTE_TRACING_ENABLED is true")
            host = self.LANGFUSE_HOST.lower()
            if "cloud.langfuse.com" in host:
                raise ValueError(
                    "LANGFUSE_HOST must point at a self-hosted Langfuse instance, "
                    "never Langfuse Cloud — tracing would otherwise send PHI "
                    "(transcript/note text) to a third party"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
