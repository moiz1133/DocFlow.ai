"""Fail-fast PHI_MODE startup self-check (Phase 7).

`run_startup_safety_checks` is called from app/main.py's lifespan BEFORE
the app selects a transcriber/note generator or serves any traffic.
Raising here aborts FastAPI's lifespan startup, which crashes the
process with a non-zero exit — refusing to boot is the entire point, not
something any caller should catch and route around.

This is deliberately independent of (not a replacement for) the
per-factory guardrails already in app/transcription/factory.py,
app/notes/factory.py, and app/security/keys.py: those stop a single
vendor/provider from being misselected; this stops the *combination* of
settings from ever reaching a running server, and is the one place that
logs a PHI-free banner of which controls are actually engaged.
"""

import logging

from app.config import Settings

logger = logging.getLogger(__name__)


class StartupSafetyCheckFailed(Exception):
    """Raised to abort application startup."""


def run_startup_safety_checks(settings: Settings) -> None:
    refusals: list[str] = []

    # Phase 1's rule, re-asserted here as an explicit, logged Phase 7
    # control rather than trusted to only ever be enforced by Settings'
    # own _validate_prod_debug validator — defense in depth: if that
    # validator is ever weakened, this still catches it.
    if settings.ENV == "prod" and settings.DEBUG:
        refusals.append("ENV=prod with DEBUG=true")

    if settings.ENV == "prod" and settings.PHI_MODE == "synthetic":
        refusals.append(
            "ENV=prod with PHI_MODE=synthetic — synthetic-data mode must never run in production"
        )

    if settings.PHI_MODE == "real":
        if settings.KEY_PROVIDER == "local":
            refusals.append(
                "PHI_MODE=real with KEY_PROVIDER=local — a real KMS-backed provider is required"
            )
        if settings.TRANSCRIBER_VENDOR == "mock":
            refusals.append("PHI_MODE=real with TRANSCRIBER_VENDOR=mock")
        if settings.NOTE_GENERATOR_VENDOR == "mock":
            refusals.append("PHI_MODE=real with NOTE_GENERATOR_VENDOR=mock")
        if settings.TRANSCRIBER_VENDOR == "openai" and not settings.OPENAI_API_KEY:
            refusals.append("PHI_MODE=real with TRANSCRIBER_VENDOR=openai but no OPENAI_API_KEY")
        if settings.NOTE_GENERATOR_VENDOR == "openai" and not settings.OPENAI_API_KEY:
            refusals.append("PHI_MODE=real with NOTE_GENERATOR_VENDOR=openai but no OPENAI_API_KEY")
        if not settings.ENFORCE_TLS:
            refusals.append("PHI_MODE=real with ENFORCE_TLS=false")
        if settings.ENV == "prod" and settings.ENFORCE_TLS and not settings.TRUST_PROXY_HEADERS:
            refusals.append(
                "PHI_MODE=real with ENV=prod, ENFORCE_TLS=true, and "
                "TRUST_PROXY_HEADERS=false — if this deployment terminates TLS at a "
                "load balancer (the normal case), every request will be incorrectly "
                "rejected as insecure; set TRUST_PROXY_HEADERS=true if so, or "
                "terminate TLS in-process if not"
            )
        if (
            settings.TRANSCRIPT_RETENTION_DEFAULT == "always"
            and not settings.ALLOW_BLANKET_RETENTION
        ):
            refusals.append(
                "PHI_MODE=real with TRANSCRIPT_RETENTION_DEFAULT=always and no "
                "ALLOW_BLANKET_RETENTION opt-in"
            )

    # Phase 8: LLM tracing (app/ops/tracing.py). Re-asserts, at startup,
    # the same two rules app/config.py's validators already make
    # impossible to construct a Settings instance with — defense in
    # depth, same shape as every other re-checked-here Phase 7 rule
    # above.
    if settings.NOTE_TRACING_ENABLED and (
        not settings.LANGFUSE_HOST or "cloud.langfuse.com" in settings.LANGFUSE_HOST.lower()
    ):
        refusals.append(
            "NOTE_TRACING_ENABLED=true with no self-hosted LANGFUSE_HOST — "
            "Langfuse Cloud is forbidden (would send PHI to a third party)"
        )
    if settings.TRACE_INCLUDE_CONTENT and (settings.PHI_MODE == "real" or settings.ENV == "prod"):
        refusals.append(
            "TRACE_INCLUDE_CONTENT=true with PHI_MODE=real or ENV=prod — "
            "content-inclusive traces are for synthetic/dev debugging only"
        )

    if refusals:
        for reason in refusals:
            logger.error("startup safety check failed", extra={"reason": reason})
        raise StartupSafetyCheckFailed("Refusing to start: " + "; ".join(refusals))

    _log_engaged_controls(settings)


def _log_engaged_controls(settings: Settings) -> None:
    """PHI-free banner of which controls are ENGAGED — never logs secrets
    (key material, API keys), only which options are selected. Meant to
    make it obvious at a glance, in prod startup logs, which controls are
    actually active rather than assumed.
    """
    logger.info(
        "HIPAA controls engaged",
        extra={
            "env": settings.ENV,
            "phi_mode": settings.PHI_MODE,
            "encryption_key_provider": settings.KEY_PROVIDER,
            "encryption_key_version": settings.ENCRYPTION_KEY_VERSION,
            "tls_enforced": settings.ENFORCE_TLS,
            "trust_proxy_headers": settings.TRUST_PROXY_HEADERS,
            "transcript_retention_default": settings.TRANSCRIPT_RETENTION_DEFAULT,
            "allow_blanket_retention": settings.ALLOW_BLANKET_RETENTION,
            "transcriber_vendor": settings.TRANSCRIBER_VENDOR,
            "note_generator_vendor": settings.NOTE_GENERATOR_VENDOR,
            "ratelimit_enabled": settings.RATELIMIT_ENABLED,
            "metrics_enabled": settings.METRICS_ENABLED,
            # Truthy check, not `is not None`: an unset env var parses as
            # "" here (same convention as OPENAI_API_KEY elsewhere), and
            # app/api/metrics.py's own gate is also a truthy check — this
            # must match that exactly or the banner would claim
            # protection the route doesn't actually enforce.
            "metrics_auth_required": bool(settings.METRICS_AUTH_TOKEN),
            "tracing_enabled": settings.NOTE_TRACING_ENABLED,
            "trace_include_content": settings.TRACE_INCLUDE_CONTENT,
            "purge_dry_run": settings.PURGE_DRY_RUN,
        },
    )
