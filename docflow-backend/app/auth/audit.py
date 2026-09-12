"""Audit-log helper for auth events.

metadata must stay PHI-free — see app/models/audit_log.py. Auth events
never carry more than the token/request bookkeeping fields below.
"""

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.models.enums import AuditAction


async def record_auth_event(
    session: AsyncSession,
    *,
    practice_id: uuid.UUID,
    action: AuditAction,
    actor_user_id: uuid.UUID | None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    entry = AuditLog(
        practice_id=practice_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type="auth",
        resource_id=actor_user_id,
        ip_address=request.client.host if request is not None and request.client else None,
        user_agent=request.headers.get("user-agent") if request is not None else None,
        metadata_=metadata,
    )
    session.add(entry)
    await session.flush()
