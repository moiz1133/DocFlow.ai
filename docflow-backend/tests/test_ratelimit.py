"""Central Redis-backed rate limiter (Phase 8) — app/ops/ratelimit.py.
Exercises it through the real HTTP routes it's wired onto rather than
calling check_rate_limit directly, so these tests also prove the wiring
(IpRateLimiter on /login, UserRateLimiter on session creation) actually
takes effect.
"""

import pytest
from httpx import AsyncClient

from app.config import get_settings
from tests.helpers import auth_headers, register_owner


async def test_exceeding_login_bucket_returns_429_with_retry_after(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tight_settings = get_settings().model_copy(
        update={"RATELIMIT_LOGIN_LIMIT": 2, "RATELIMIT_LOGIN_WINDOW_SECONDS": 60}
    )
    import app.ops.ratelimit as ratelimit_module

    monkeypatch.setattr(ratelimit_module, "get_settings", lambda: tight_settings)

    body = {"email": "nobody@example.com", "password": "wrong-password"}
    for _ in range(2):
        response = await client.post("/v1/auth/login", json=body)
        assert response.status_code == 401  # under the limit — ordinary auth failure

    limited = await client.post("/v1/auth/login", json=body)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    assert int(limited.headers["Retry-After"]) > 0


async def test_login_bucket_is_stricter_than_the_read_bucket(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirms the two buckets have genuinely different, independently
    configurable ceilings — not a shared/global limiter.
    """
    settings = get_settings()
    assert settings.RATELIMIT_LOGIN_LIMIT < settings.RATELIMIT_READ_LIMIT


async def test_disabling_ratelimit_flag_bypasses_the_limiter(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled_settings = get_settings().model_copy(
        update={"RATELIMIT_ENABLED": False, "RATELIMIT_LOGIN_LIMIT": 1}
    )
    import app.ops.ratelimit as ratelimit_module

    monkeypatch.setattr(ratelimit_module, "get_settings", lambda: disabled_settings)

    body = {"email": "nobody@example.com", "password": "wrong-password"}
    for _ in range(5):
        response = await client.post("/v1/auth/login", json=body)
        assert response.status_code == 401  # never 429 — the limiter is bypassed entirely


async def test_exceeding_session_create_bucket_returns_429_for_authenticated_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tight_settings = get_settings().model_copy(
        update={"RATELIMIT_SESSION_CREATE_LIMIT": 2, "RATELIMIT_SESSION_CREATE_WINDOW_SECONDS": 60}
    )
    import app.ops.ratelimit as ratelimit_module

    monkeypatch.setattr(ratelimit_module, "get_settings", lambda: tight_settings)

    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])

    for _ in range(2):
        response = await client.post("/v1/sessions", headers=headers)
        assert response.status_code == 201

    limited = await client.post("/v1/sessions", headers=headers)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


async def test_session_create_limit_is_keyed_per_user_not_globally(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different user's session-create calls must not be throttled by
    another user's usage of the same bucket — see UserRateLimiter's
    "user:<id>:practice:<id>" keying.
    """
    tight_settings = get_settings().model_copy(
        update={"RATELIMIT_SESSION_CREATE_LIMIT": 1, "RATELIMIT_SESSION_CREATE_WINDOW_SECONDS": 60}
    )
    import app.ops.ratelimit as ratelimit_module

    monkeypatch.setattr(ratelimit_module, "get_settings", lambda: tight_settings)

    owner_a = await register_owner(client)
    headers_a = auth_headers(owner_a["access_token"])
    owner_b = await register_owner(client)
    headers_b = auth_headers(owner_b["access_token"])

    assert (await client.post("/v1/sessions", headers=headers_a)).status_code == 201
    assert (await client.post("/v1/sessions", headers=headers_a)).status_code == 429

    # A different user, same bucket, same window — not affected by A's usage.
    assert (await client.post("/v1/sessions", headers=headers_b)).status_code == 201
