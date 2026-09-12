"""require_role: owner-only routes admit owners and block staff.

GET /v1/users/{user_id} (require_role(UserRole.owner)) is the demo route
that exists specifically to exercise this end-to-end over real HTTP.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.models import User
from app.models.enums import UserRole
from tests.helpers import auth_headers, register_owner

STAFF_PASSWORD = "staff-strong-password-1"


async def _add_staff_user(
    admin_sessionmaker: async_sessionmaker[AsyncSession], *, practice_id: str, suffix: str
) -> str:
    email = f"staff-{suffix}@example.com"
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            User(
                practice_id=practice_id,
                email=email,
                hashed_password=hash_password(STAFF_PASSWORD),
                full_name="Test Staff",
                role=UserRole.staff,
            )
        )
    return email


async def test_owner_can_access_owner_only_route(client: AsyncClient) -> None:
    registered = await register_owner(client)
    me = await client.get("/v1/auth/me", headers=auth_headers(registered["access_token"]))
    own_id = me.json()["id"]

    response = await client.get(
        f"/v1/users/{own_id}", headers=auth_headers(registered["access_token"])
    )
    assert response.status_code == 200, response.text
    assert response.json()["email"] == registered["email"]


async def test_staff_is_blocked_from_owner_only_route(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    registered = await register_owner(client)
    me = await client.get("/v1/auth/me", headers=auth_headers(registered["access_token"]))
    own_id = me.json()["id"]
    practice_id = me.json()["practice_id"]

    staff_email = await _add_staff_user(
        admin_sessionmaker, practice_id=practice_id, suffix=registered["email"].split("@")[0]
    )

    login = await client.post(
        "/v1/auth/login", json={"email": staff_email, "password": STAFF_PASSWORD}
    )
    assert login.status_code == 200, login.text
    staff_access_token = login.json()["access_token"]

    response = await client.get(f"/v1/users/{own_id}", headers=auth_headers(staff_access_token))
    assert response.status_code == 403
