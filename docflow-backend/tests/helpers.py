"""Small helpers shared across auth test modules."""

import uuid
from typing import Any

from httpx import AsyncClient

DEFAULT_PASSWORD = "correct horse battery staple"


async def register_owner(
    client: AsyncClient,
    *,
    password: str = DEFAULT_PASSWORD,
    full_name: str = "Test Owner",
) -> dict[str, Any]:
    """Registers a fresh practice + owner user; returns the JSON response
    (access_token, refresh_token, token_type)."""
    suffix = uuid.uuid4().hex[:8]
    email = f"owner-{suffix}@example.com"
    response = await client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": full_name,
            "practice_name": f"Test Practice {suffix}",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    body["email"] = email
    body["password"] = password
    return body


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}
