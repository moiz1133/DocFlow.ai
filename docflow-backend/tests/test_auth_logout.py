"""Logout revokes the presented refresh token; it can't be used again."""

from httpx import AsyncClient

from tests.helpers import auth_headers, register_owner


async def test_logout_revokes_the_refresh_token(client: AsyncClient) -> None:
    registered = await register_owner(client)

    logout = await client.post(
        "/v1/auth/logout",
        headers=auth_headers(registered["access_token"]),
        json={"refresh_token": registered["refresh_token"]},
    )
    assert logout.status_code == 200, logout.text

    refresh_attempt = await client.post(
        "/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
    )
    assert refresh_attempt.status_code == 401
