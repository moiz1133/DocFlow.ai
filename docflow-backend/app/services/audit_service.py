"""The single choke point for writing a PHI-access audit entry — reads
as well as writes (Phase 7; Phases 2-6 only audited writes).

HIPAA requires logging PHI *access*, not just mutation: reading a
transcript, generating/reading a note, exporting, etc. must all produce
an audit_logs row, same as creating one did already. Call this ONCE per
logical operation, not once per field — see
app/security/encrypted_type.py's module docstring for why the
encryption layer itself is deliberately NOT the hook point (that would
fire once per decrypted column per row: noisy, and it would mean this
module would need to run inside SQLAlchemy's result-processing path,
which has no natural place to carry "what operation is this" context).

metadata stays strictly PHI-free — resource ids, counts, provider names,
never transcript/note text (see app/models/audit_log.py). request_meta
carries ip_address/user_agent when the caller has them to hand (a
FastAPI Request or a WebSocket both expose enough to build one — see
_request_meta_from_request below); pass None when there's no request in
scope (e.g. a background/Celery-style caller per
app/services/note_service.py's docstring).
"""

import uuid
from typing import TypedDict

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.models.enums import AuditAction


class RequestMeta(TypedDict, total=False):
    ip_address: str | None
    user_agent: str | None


async def record_phi_access(
    db: AsyncSession,
    *,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: AuditAction,
    resource_type: str,
    resource_id: uuid.UUID | None,
    metadata: dict[str, object] | None = None,
    request_meta: RequestMeta | None = None,
) -> None:
    db.add(
        AuditLog(
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=(request_meta or {}).get("ip_address"),
            user_agent=(request_meta or {}).get("user_agent"),
            metadata_=metadata,
        )
    )
    await db.flush()


def request_meta_from_request(request: Request) -> RequestMeta:
    """Extracts what record_phi_access needs from a FastAPI Request —
    the REST-route equivalent of app/api/sessions.py's WS handler, which
    has no Request object and passes request_meta=None instead (a
    WebSocket's `.client`/`.headers` could support the same extraction
    if a later phase wants WS-sourced PHI access audited with IP/UA too).
    """
    return {
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
