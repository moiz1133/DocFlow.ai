"""Application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.notes import router as notes_router
from app.api.sessions import router as sessions_router
from app.api.users import router as users_router
from app.config import get_settings
from app.logging import configure_logging
from app.notes.factory import get_note_generator
from app.ops.http_metrics import MetricsMiddleware
from app.security.startup_checks import run_startup_safety_checks
from app.security.transport import SecurityHeadersMiddleware, TLSEnforcementMiddleware
from app.transcription.factory import get_transcriber

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger.info("startup complete", extra={"env": settings.ENV, "phi_mode": settings.PHI_MODE})
    # Refuses to finish starting (crashes the process) if any HIPAA
    # control is disengaged for the current ENV/PHI_MODE combination —
    # see app/security/startup_checks.py. Runs before vendor selection
    # below so a misconfigured PHI_MODE=real deployment never gets far
    # enough to even try building a real transcriber/note generator.
    run_startup_safety_checks(settings)
    # Selecting the transcriber here (rather than lazily, on first use)
    # means a misconfigured TRANSCRIBER_VENDOR (see
    # app/transcription/factory.py's guardrails — e.g. ENV=prod with
    # vendor="mock") fails application startup immediately instead of
    # surfacing as a 500 on some patient's first recorded visit.
    # get_transcriber() itself logs the selected provider.
    get_transcriber()
    # Same fail-fast reasoning for NOTE_GENERATOR_VENDOR (see
    # app/notes/factory.py's guardrails) — get_note_generator() itself
    # logs the selected provider.
    get_note_generator()
    yield


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(title="DocFlow.ai Backend", version="0.1.0", lifespan=lifespan)

    settings = get_settings()
    origins = [*settings.CORS_WEB_ORIGINS, *settings.CORS_EXTENSION_ORIGINS]
    # Explicit origins only, never "*": the extension's chrome-extension://
    # origin and the web app's origin are both known ahead of time, and
    # browsers reject a wildcard origin combined with allow_credentials
    # anyway. The Authorization header carries the bearer token for all
    # three clients (extension, web, mobile) — nothing here relies on
    # cookies, but allow_credentials stays on for forward-compatibility
    # with any future cookie-based flow the web app might add.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    # HTTP request metrics (Phase 8) — see app/ops/http_metrics.py and
    # GET /metrics (app/api/metrics.py). Registered before the two
    # security middlewares below so TLSEnforcementMiddleware (outermost —
    # see the comment on it) stays the very first thing an insecure
    # request meets; a request TLS rejects is never counted here.
    app.add_middleware(MetricsMiddleware)
    # TLS enforcement (in transit) and security headers — see
    # app/security/transport.py. Registered regardless of ENV; what
    # actually happens is entirely governed by settings.ENFORCE_TLS
    # (defaults True; .env.example turns it off for local dev). Starlette
    # runs middleware in reverse-of-registration order on the way in, so
    # TLSEnforcementMiddleware (added last here) is the outermost layer —
    # an insecure request is rejected before CORS or routing ever see it.
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(TLSEnforcementMiddleware, settings=settings)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(sessions_router)
    app.include_router(notes_router)
    return app


app = create_app()
