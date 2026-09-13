"""TLSEnforcementMiddleware / SecurityHeadersMiddleware — see
app/security/transport.py. Tested against a minimal standalone Starlette
app (not the real app/main.py one) so each test can supply its own
Settings without needing to reconstruct the whole application per
scenario.
"""

import base64

import pytest
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from app.config import Settings
from app.security.transport import SecurityHeadersMiddleware, TLSEnforcementMiddleware

_LOCAL_KEY = base64.b64encode(b"k" * 32).decode()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "SECRET_KEY": "a" * 40,
        "JWT_SECRET": "b" * 40,
        "DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "APP_DATABASE_URL": "postgresql+asyncpg://a:b@localhost/x",
        "REDIS_URL": "redis://localhost",
        "LOCAL_ENCRYPTION_KEY": _LOCAL_KEY,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _ws_echo(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.close()


def _build_app(settings: Settings) -> Starlette:
    app = Starlette(routes=[Route("/", _ok), WebSocketRoute("/ws", _ws_echo)])
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(TLSEnforcementMiddleware, settings=settings)
    return app


async def test_enforce_tls_false_allows_plain_http() -> None:
    settings = _settings(ENFORCE_TLS=False)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200


async def test_enforce_tls_true_rejects_plain_http() -> None:
    settings = _settings(ENFORCE_TLS=True, TRUST_PROXY_HEADERS=False)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 400


async def test_enforce_tls_true_allows_https_scheme() -> None:
    settings = _settings(ENFORCE_TLS=True)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="https://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200


async def test_trusted_proxy_header_allows_when_configured() -> None:
    settings = _settings(ENFORCE_TLS=True, TRUST_PROXY_HEADERS=True)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200


async def test_proxy_header_ignored_when_not_trusted() -> None:
    settings = _settings(ENFORCE_TLS=True, TRUST_PROXY_HEADERS=False)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 400


async def test_hsts_header_present_when_tls_enforced() -> None:
    settings = _settings(ENFORCE_TLS=True)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="https://test"
    ) as client:
        response = await client.get("/")
    assert "max-age=63072000" in response.headers["strict-transport-security"]
    assert "includeSubDomains" in response.headers["strict-transport-security"]


async def test_hsts_header_absent_when_tls_not_enforced() -> None:
    settings = _settings(ENFORCE_TLS=False)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert "strict-transport-security" not in response.headers


async def test_baseline_security_headers_always_present() -> None:
    settings = _settings(ENFORCE_TLS=False)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_ws_rejected_when_tls_enforced_and_scheme_is_plain_ws() -> None:
    settings = _settings(ENFORCE_TLS=True, TRUST_PROXY_HEADERS=False)
    async with AsyncClient(
        transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
    ) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            async with aconnect_ws("/ws", client):
                pass
    assert exc_info.value.code == 4400


async def test_ws_allowed_over_wss_scheme() -> None:
    settings = _settings(ENFORCE_TLS=True)
    ws: AsyncWebSocketSession
    async with (
        AsyncClient(
            transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="https://test"
        ) as client,
        aconnect_ws("/ws", client) as ws,
    ):
        # The server-side echo route accepts then immediately closes
        # cleanly (code 1000) — reaching that at all (rather than our
        # custom 4400) proves the connection was treated as secure.
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive()
    assert exc_info.value.code == 1000


async def test_ws_allowed_when_tls_not_enforced() -> None:
    settings = _settings(ENFORCE_TLS=False)
    ws: AsyncWebSocketSession
    async with (
        AsyncClient(
            transport=ASGIWebSocketTransport(app=_build_app(settings)), base_url="http://test"
        ) as client,
        aconnect_ws("/ws", client) as ws,
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive()
    assert exc_info.value.code == 1000
