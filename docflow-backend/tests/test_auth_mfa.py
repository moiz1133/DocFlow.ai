"""MFA path: setup -> confirm -> login now requires TOTP -> exchange."""

import pyotp
from httpx import AsyncClient

from tests.helpers import auth_headers, register_owner


async def test_mfa_setup_confirm_then_login_requires_totp(client: AsyncClient) -> None:
    registered = await register_owner(client)

    setup = await client.post(
        "/v1/auth/mfa/setup", headers=auth_headers(registered["access_token"])
    )
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["provisioning_uri"].startswith("otpauth://")

    confirm = await client.post(
        "/v1/auth/mfa/verify",
        headers=auth_headers(registered["access_token"]),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["mfa_enabled"] is True
    assert confirm.json()["access_token"] is None

    # Password-only login must now stop short of full tokens.
    login = await client.post(
        "/v1/auth/login",
        json={"email": registered["email"], "password": registered["password"]},
    )
    assert login.status_code == 200
    login_body = login.json()
    assert login_body["mfa_required"] is True
    assert login_body["access_token"] is None
    assert login_body["mfa_pending_token"]

    exchange = await client.post(
        "/v1/auth/mfa/verify",
        headers=auth_headers(login_body["mfa_pending_token"]),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert exchange.status_code == 200, exchange.text
    exchange_body = exchange.json()
    assert exchange_body["mfa_enabled"] is True
    assert exchange_body["access_token"]
    assert exchange_body["refresh_token"]

    me = await client.get("/v1/auth/me", headers=auth_headers(exchange_body["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == registered["email"]


async def test_mfa_pending_token_rejects_wrong_code(client: AsyncClient) -> None:
    registered = await register_owner(client)

    setup = await client.post(
        "/v1/auth/mfa/setup", headers=auth_headers(registered["access_token"])
    )
    secret = setup.json()["secret"]
    await client.post(
        "/v1/auth/mfa/verify",
        headers=auth_headers(registered["access_token"]),
        json={"code": pyotp.TOTP(secret).now()},
    )

    login = await client.post(
        "/v1/auth/login",
        json={"email": registered["email"], "password": registered["password"]},
    )
    mfa_pending_token = login.json()["mfa_pending_token"]

    wrong_code = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
    response = await client.post(
        "/v1/auth/mfa/verify",
        headers=auth_headers(mfa_pending_token),
        json={"code": wrong_code},
    )
    assert response.status_code == 401
