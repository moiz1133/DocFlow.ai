"""POST /v1/sessions and GET /v1/sessions/{id} — session creation and
status lookup, independent of the streaming/finalize paths.
"""

import uuid

from httpx import AsyncClient

from tests.helpers import auth_headers, register_owner


async def test_create_session_returns_an_id(client: AsyncClient) -> None:
    owner = await register_owner(client)
    response = await client.post("/v1/sessions", headers=auth_headers(owner["access_token"]))

    assert response.status_code == 201, response.text
    body = response.json()
    assert uuid.UUID(body["session_id"])


async def test_get_session_reports_created_status_and_no_transcript(client: AsyncClient) -> None:
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    create_response = await client.post("/v1/sessions", headers=headers)
    session_id = create_response.json()["session_id"]

    response = await client.get(f"/v1/sessions/{session_id}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == session_id
    assert body["status"] == "created"
    assert body["transcript_exists"] is False


async def test_get_session_404_for_unknown_id(client: AsyncClient) -> None:
    owner = await register_owner(client)
    response = await client.get(
        f"/v1/sessions/{uuid.uuid4()}", headers=auth_headers(owner["access_token"])
    )
    assert response.status_code == 404


async def test_get_session_404_for_another_practices_session(client: AsyncClient) -> None:
    """A guessed id belonging to another tenant comes back 404, not 403 —
    RLS hides it entirely, same pattern as app/api/users.py's get_user.
    """
    owner_a = await register_owner(client)
    owner_b = await register_owner(client)

    create_response = await client.post(
        "/v1/sessions", headers=auth_headers(owner_a["access_token"])
    )
    session_id = create_response.json()["session_id"]

    response = await client.get(
        f"/v1/sessions/{session_id}", headers=auth_headers(owner_b["access_token"])
    )
    assert response.status_code == 404


async def test_create_session_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/v1/sessions")
    assert response.status_code == 401
