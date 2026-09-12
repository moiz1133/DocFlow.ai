"""Protected-route access is refused for any invalid bearer token: none
presented, expired, or the wrong token type.
"""

import uuid

from httpx import AsyncClient

from app.auth.tokens import create_access_token
from app.models.enums import UserRole
from tests.helpers import auth_headers, register_owner


async def test_me_without_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/v1/auth/me")
    assert response.status_code == 401


async def test_me_with_garbage_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/v1/auth/me", headers=auth_headers("not-a-real-jwt"))
    assert response.status_code == 401


async def test_me_with_expired_access_token_is_401(client: AsyncClient) -> None:
    registered = await register_owner(client)
    me = await client.get("/v1/auth/me", headers=auth_headers(registered["access_token"]))
    profile = me.json()

    expired_token = create_access_token(
        uuid.UUID(profile["id"]),
        uuid.UUID(profile["practice_id"]),
        UserRole(profile["role"]),
        ttl_minutes=-1,
    )

    response = await client.get("/v1/auth/me", headers=auth_headers(expired_token))
    assert response.status_code == 401


async def test_me_with_refresh_token_instead_of_access_is_401(client: AsyncClient) -> None:
    """A well-formed, currently-valid *refresh* token must not work as an
    access token — decode_access_token enforces claim `type == "access"`.
    """
    registered = await register_owner(client)

    response = await client.get("/v1/auth/me", headers=auth_headers(registered["refresh_token"]))
    assert response.status_code == 401
