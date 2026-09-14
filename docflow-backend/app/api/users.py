"""Minimal colleague-lookup route.

Exists primarily to prove two things end-to-end over real HTTP, not as a
business feature (full user management is a later phase's concern):
require_role's RBAC (owner-only), and that RLS actually blocks a guessed
user_id belonging to a different tenant. `users` (unlike `practices`,
which is the tenant root and deliberately not RLS-scoped) carries
practice_id and is genuinely RLS-protected, which is what makes fetching
*by id* here a meaningful proof of cross-tenant isolation.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import FORBIDDEN, NOT_FOUND, UNAUTHORIZED, merge_responses
from app.auth.dependencies import get_tenant_session, require_role
from app.models import User
from app.models.enums import UserRole

router = APIRouter(prefix="/v1/users", tags=["users"])

_require_owner = require_role(UserRole.owner)


class UserSummaryResponse(BaseModel):
    id: uuid.UUID
    email: str = Field(examples=["colleague@example-clinic.test"])
    full_name: str = Field(examples=["Dr. Jamie Rivera"])
    role: UserRole


@router.get(
    "/{user_id}",
    responses=merge_responses(UNAUTHORIZED, FORBIDDEN, NOT_FOUND),
    summary="Look up a colleague in the caller's own practice (owner-only)",
)
async def get_user(
    user_id: uuid.UUID,
    caller: Annotated[User, Depends(_require_owner)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> UserSummaryResponse:
    # No manual `target.practice_id == caller.practice_id` check on
    # purpose: the point is that RLS (scoped by get_tenant_session to the
    # caller's own practice) makes another tenant's row invisible
    # regardless — a guessed user_id from a different practice comes back
    # 404, not 200 with someone else's data.
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return UserSummaryResponse(
        id=target.id, email=target.email, full_name=target.full_name, role=target.role
    )
