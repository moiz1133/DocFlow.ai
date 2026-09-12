"""register -> login -> /me happy path, plus the generic-error and
duplicate-email edges around it.
"""

from httpx import AsyncClient

from tests.helpers import auth_headers, register_owner


async def test_register_login_me_happy_path(client: AsyncClient) -> None:
    registered = await register_owner(client)

    me = await client.get("/v1/auth/me", headers=auth_headers(registered["access_token"]))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == registered["email"]
    assert body["role"] == "owner"
    assert body["is_active"] is True
    assert body["mfa_enabled"] is False

    login = await client.post(
        "/v1/auth/login",
        json={"email": registered["email"], "password": registered["password"]},
    )
    assert login.status_code == 200, login.text
    login_body = login.json()
    assert login_body["mfa_required"] is False
    assert login_body["access_token"]
    assert login_body["refresh_token"]

    me_after_login = await client.get(
        "/v1/auth/me", headers=auth_headers(login_body["access_token"])
    )
    assert me_after_login.status_code == 200
    assert me_after_login.json()["email"] == registered["email"]


async def test_duplicate_email_registration_is_rejected(client: AsyncClient) -> None:
    registered = await register_owner(client)

    response = await client.post(
        "/v1/auth/register",
        json={
            "email": registered["email"],
            "password": "another-strong-password",
            "full_name": "Someone Else",
            "practice_name": "Another Practice",
        },
    )
    assert response.status_code == 409


async def test_login_wrong_password_is_generic(client: AsyncClient) -> None:
    registered = await register_owner(client)

    response = await client.post(
        "/v1/auth/login",
        json={"email": registered["email"], "password": "definitely-wrong"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


async def test_login_unknown_email_is_generic_and_same_message(client: AsyncClient) -> None:
    registered = await register_owner(client)

    wrong_password_resp = await client.post(
        "/v1/auth/login",
        json={"email": registered["email"], "password": "definitely-wrong"},
    )
    unknown_email_resp = await client.post(
        "/v1/auth/login",
        json={"email": "nobody-here@example.com", "password": "whatever12345"},
    )

    assert wrong_password_resp.status_code == unknown_email_resp.status_code == 401
    assert wrong_password_resp.json()["detail"] == unknown_email_resp.json()["detail"]
