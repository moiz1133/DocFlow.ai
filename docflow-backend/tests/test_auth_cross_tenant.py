"""Cross-tenant isolation holds for an authenticated request — RLS via
get_tenant_session/set_tenant, exercised over real HTTP with real JWTs,
not just against a raw DB session (see tests/test_tenant_isolation.py for
the Phase 2, no-auth version of this guarantee).
"""

from httpx import AsyncClient

from tests.helpers import auth_headers, register_owner


async def test_authenticated_request_cannot_read_another_practices_user(
    client: AsyncClient,
) -> None:
    practice_a_owner = await register_owner(client)
    practice_b_owner = await register_owner(client)

    me_b = await client.get("/v1/auth/me", headers=auth_headers(practice_b_owner["access_token"]))
    practice_b_user_id = me_b.json()["id"]

    # Practice A's owner requests Practice B's (real, existing) user id —
    # this must come back 404, not 200 with someone else's data and not a
    # 403 that would at least confirm the id is real.
    response = await client.get(
        f"/v1/users/{practice_b_user_id}", headers=auth_headers(practice_a_owner["access_token"])
    )
    assert response.status_code == 404

    # Sanity check: the same id, requested by its own owner, does resolve.
    own_lookup = await client.get(
        f"/v1/users/{practice_b_user_id}",
        headers=auth_headers(practice_b_owner["access_token"]),
    )
    assert own_lookup.status_code == 200
    assert own_lookup.json()["email"] == practice_b_owner["email"]
