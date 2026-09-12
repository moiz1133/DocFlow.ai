"""Refresh rotation, reuse detection, and idle timeout."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import decode_refresh_token
from app.models import RefreshToken
from tests.helpers import auth_headers, register_owner


async def test_refresh_rotates_both_tokens(client: AsyncClient) -> None:
    registered = await register_owner(client)

    response = await client.post(
        "/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["refresh_token"] != registered["refresh_token"]
    assert body["access_token"] != registered["access_token"]

    me = await client.get("/v1/auth/me", headers=auth_headers(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == registered["email"]


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_chain(
    client: AsyncClient,
) -> None:
    registered = await register_owner(client)

    first_rotation = await client.post(
        "/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
    )
    assert first_rotation.status_code == 200
    rotated_refresh_token = first_rotation.json()["refresh_token"]

    # Reusing the already-rotated (original) token is the attack signal.
    reuse_attempt = await client.post(
        "/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
    )
    assert reuse_attempt.status_code == 401

    # The legitimate, newly-rotated token must now be dead too — reuse
    # detection revokes the whole chain, not just the reused token.
    follow_up = await client.post("/v1/auth/refresh", json={"refresh_token": rotated_refresh_token})
    assert follow_up.status_code == 401


async def test_idle_timeout_refuses_refresh(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    registered = await register_owner(client)
    jti = decode_refresh_token(registered["refresh_token"]).jti

    stale = datetime.now(UTC) - timedelta(days=1)
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            update(RefreshToken).where(RefreshToken.jti == jti).values(last_activity_at=stale)
        )

    response = await client.post(
        "/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
    )
    assert response.status_code == 401
