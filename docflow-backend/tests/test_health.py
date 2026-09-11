"""Tests for the health/liveness/readiness endpoints."""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_liveness(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_ready_all_dependencies_up(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(_url: str) -> bool:
        return True

    monkeypatch.setattr("app.api.health._check_postgres", _ok)
    monkeypatch.setattr("app.api.health._check_redis", _ok)

    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"] == {"postgres": "ok", "redis": "ok"}


async def test_health_ready_dependency_down_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(_url: str) -> bool:
        return True

    async def _down(_url: str) -> bool:
        return False

    monkeypatch.setattr("app.api.health._check_postgres", _down)
    monkeypatch.setattr("app.api.health._check_redis", _ok)

    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["dependencies"] == {"postgres": "unavailable", "redis": "ok"}
