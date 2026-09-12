"""Application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.users import router as users_router
from app.config import get_settings
from app.logging import configure_logging
from app.transcription.factory import get_transcriber

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger.info("startup complete", extra={"env": settings.ENV, "phi_mode": settings.PHI_MODE})
    # Selecting the transcriber here (rather than lazily, on first use)
    # means a misconfigured TRANSCRIBER_VENDOR (see
    # app/transcription/factory.py's guardrails — e.g. ENV=prod with
    # vendor="mock") fails application startup immediately instead of
    # surfacing as a 500 on some patient's first recorded visit.
    # get_transcriber() itself logs the selected provider.
    get_transcriber()
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

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    return app


app = create_app()
