"""HTTP request metrics middleware (Phase 8).

BaseHTTPMiddleware (unlike app/security/transport.py's pure-ASGI
middlewares) is fine here: its silent pass-through of non-HTTP scope is
exactly what's wanted — WebSocket connections get their own gauge/
histogram instrumented directly in app/api/sessions.py's stream_session,
not here.

Cardinality safety: labels by the matched ROUTE TEMPLATE
(e.g. "/v1/sessions/{session_id}/note"), never the raw request path —
a raw path would let a client blow up this metric's cardinality just by
requesting made-up ids. A request that matches no route at all is
labeled "unmatched" rather than its raw path, for the same reason.
"""

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.ops.metrics import (
    HTTP_IN_FLIGHT_REQUESTS,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method
        HTTP_IN_FLIGHT_REQUESTS.inc()
        started_at = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            route = _route_template(request)
            HTTP_REQUEST_DURATION_SECONDS.labels(route=route, method=method).observe(
                time.monotonic() - started_at
            )
            HTTP_REQUESTS_TOTAL.labels(route=route, method=method, status="500").inc()
            raise
        finally:
            HTTP_IN_FLIGHT_REQUESTS.dec()

        route = _route_template(request)
        HTTP_REQUEST_DURATION_SECONDS.labels(route=route, method=method).observe(
            time.monotonic() - started_at
        )
        HTTP_REQUESTS_TOTAL.labels(
            route=route, method=method, status=str(response.status_code)
        ).inc()
        return response
