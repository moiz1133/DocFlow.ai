"""GET /metrics — Prometheus scrape endpoint (Phase 8).

NOT meant to be public — see app/config.py's METRICS_ENABLED/
METRICS_AUTH_TOKEN and the README's "Metrics" section. When
METRICS_AUTH_TOKEN is set, a request must present a matching
`Authorization: Bearer <token>` header; when unset, this endpoint relies
entirely on network policy (an internal-only ingress/firewall rule) to
keep it off the public internet — a Prometheus scrape target, not a
user-facing API.
"""

import hmac

from fastapi import APIRouter, HTTPException, Request, Response, status
from prometheus_client import generate_latest

from app.config import get_settings
from app.ops.metrics import CONTENT_TYPE_LATEST, REGISTRY

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    settings = get_settings()
    if not settings.METRICS_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    if settings.METRICS_AUTH_TOKEN:
        provided = request.headers.get("authorization", "")
        expected = f"Bearer {settings.METRICS_AUTH_TOKEN}"
        # Constant-time comparison: this token guards an operational
        # endpoint, not PHI, but there is no reason to accept a
        # timing-observable comparison when a constant-time one is free.
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
