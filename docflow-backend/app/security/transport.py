"""TLS enforcement and security headers — Phase 7's in-transit control.

TLSEnforcementMiddleware is a pure ASGI middleware, deliberately NOT
based on Starlette's BaseHTTPMiddleware: that base class only ever sees
`scope["type"] == "http"` and silently passes websocket connections
straight through untouched, which would leave `ws://` reachable in prod
no matter what this middleware did. Implementing directly against the
ASGI interface is the only way to reject both an insecure HTTP request
and an insecure (`ws://`, not `wss://`) WebSocket handshake with one
mechanism.

Both middlewares are still registered even outside prod (see
app/main.py) but ENFORCE_TLS defaults to True and only .env.example's
dev override turns it off — so what actually runs in dev is governed by
that setting, not a separate code path here.
"""

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings

logger = logging.getLogger(__name__)

# Non-standard 4xxx-range close code (the 4000-4999 band is reserved for
# application use by the WebSocket spec) — mirrors app/api/sessions.py's
# existing custom codes (4401, 4404, 4413) rather than reusing a generic
# standard one.
_WS_CLOSE_TLS_REQUIRED = 4400


def _is_secure_scope(scope: Scope, settings: Settings) -> bool:
    if scope.get("scheme") in ("https", "wss"):
        return True
    if settings.TRUST_PROXY_HEADERS:
        headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
        forwarded_proto = headers.get(b"x-forwarded-proto", b"").decode("latin-1")
        first = forwarded_proto.split(",")[0].strip().lower()
        return first == "https"
    return False


class TLSEnforcementMiddleware:
    """Rejects any HTTP or WebSocket connection that didn't arrive over
    TLS when settings.ENFORCE_TLS is True.

    HTTP gets a flat 400, not a redirect to https://: a redirect response
    still has to be sent over the same insecure connection first, which
    is exactly the exposure this exists to close, and it would train
    clients to expect a working (if suboptimal) response over plain HTTP.
    WebSocket gets a close with a custom 4400 code, before ever accepting
    the connection.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self._settings.ENFORCE_TLS
            or scope["type"] not in ("http", "websocket")
            or _is_secure_scope(scope, self._settings)
        ):
            await self._app(scope, receive, send)
            return

        if scope["type"] == "http":
            response = JSONResponse({"detail": "HTTPS is required"}, status_code=400)
            await response(scope, receive, send)
            return

        # websocket: consume the mandatory "websocket.connect" event
        # before responding, per the ASGI websocket protocol, then close
        # without ever accepting.
        await receive()
        close_message: Message = {"type": "websocket.close", "code": _WS_CLOSE_TLS_REQUIRED}
        await send(close_message)


class SecurityHeadersMiddleware:
    """Adds baseline security headers to every HTTP response; adds HSTS
    only when TLS is actually being enforced (an HSTS header over a
    connection that isn't guaranteed HTTPS is actively misleading).
    HTTP-only like TLSEnforcementMiddleware's HTTP branch — a completed
    WebSocket upgrade has no meaningful "response headers" to decorate
    the way a normal HTTP response does.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def _send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"referrer-policy", b"no-referrer"))
                if self._settings.ENFORCE_TLS:
                    headers.append(
                        (b"strict-transport-security", b"max-age=63072000; includeSubDomains")
                    )
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, _send_with_headers)
