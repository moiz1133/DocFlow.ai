"""Authentication/authorization FastAPI dependencies.

get_current_user does double duty: verifying the caller's identity AND
(via get_tenant_session) wiring Row-Level Security for the rest of the
request. There is deliberately no way to get an authenticated User without
also getting a tenant-scoped session — see get_tenant_session's docstring.
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import AccessTokenClaims, TokenError, decode_access_token
from app.db.session import get_session, set_tenant
from app.models import User
from app.models.enums import UserRole

_bearer_scheme = HTTPBearer(auto_error=False)

# owner sees everything a clinician can, clinician sees everything staff
# can. require_role(*roles) admits a user whose rank is >= the *lowest*
# rank among the roles passed, so require_role(UserRole.clinician) means
# "clinician or above", not "exactly clinician".
_ROLE_RANK = {UserRole.staff: 1, UserRole.clinician: 2, UserRole.owner: 3}


def unauthorized_error(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)],
) -> str:
    if credentials is None:
        raise unauthorized_error()
    return credentials.credentials


async def get_access_claims(token: Annotated[str, Depends(get_bearer_token)]) -> AccessTokenClaims:
    try:
        return decode_access_token(token)
    except TokenError:
        raise unauthorized_error("Invalid or expired token") from None


async def get_tenant_session(
    claims: Annotated[AccessTokenClaims, Depends(get_access_claims)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncIterator[AsyncSession]:
    """A session with RLS already scoped to the caller's practice.

    Every authenticated, DB-touching route should depend on this (directly,
    or transitively via get_current_user) rather than on get_session
    directly. A route that used get_session directly would query with no
    tenant GUC set — RLS's fail-closed default turns that into "see
    nothing" rather than "see everyone's data", but it's still not what a
    protected route wants.
    """
    async with session.begin():
        await set_tenant(session, claims.practice_id)
        yield session


async def get_current_user(
    claims: Annotated[AccessTokenClaims, Depends(get_access_claims)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> User:
    user = await session.get(User, claims.user_id)
    # user.practice_id == claims.practice_id is redundant with RLS (a row
    # from another tenant should never come back at all) but costs
    # nothing and catches a class of bug where RLS silently didn't apply.
    if user is None or not user.is_active or user.practice_id != claims.practice_id:
        raise unauthorized_error("Invalid or expired token")
    return user


def require_role(*roles: UserRole) -> Callable[[User], Coroutine[Any, Any, User]]:
    """Admits a user whose role rank is >= the lowest rank in `roles`.

    owner ⊇ clinician ⊇ staff: an owner-only route is
    require_role(UserRole.owner); a clinician-or-owner route is
    require_role(UserRole.clinician).
    """
    minimum_rank = min(_ROLE_RANK[role] for role in roles)

    async def _dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if _ROLE_RANK[user.role] < minimum_rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return _dependency
