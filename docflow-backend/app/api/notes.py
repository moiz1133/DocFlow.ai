"""SOAP note generation endpoint, mounted at /v1/sessions (extends the
Phase 5 session resource with one more sub-resource: its generated note).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.models import User
from app.models.enums import NoteStatus
from app.services.audit_service import request_meta_from_request
from app.services.note_service import (
    SessionNotFoundError,
    TranscriptNotReadyError,
    generate_note_for_session,
)

router = APIRouter(prefix="/v1/sessions", tags=["notes"])


class NoteOut(BaseModel):
    id: uuid.UUID
    status: NoteStatus
    subjective: str | None
    objective: str | None
    assessment: str | None
    plan: str | None
    # On success this is the composed SOAP text; when degraded=True, this
    # is the raw transcript — see app/services/note_service.py's
    # DEGRADATION POLICY. Either way, the caller always has something
    # readable here, regardless of is_retained (see below).
    full_text: str | None
    provider: str | None
    model: str | None
    prompt_version: str | None
    version: int
    degraded: bool
    # Whether this content was actually written to the notes table (the
    # Phase 7 consent gate — app/security/consent.py). False means the
    # persisted row is a minimal stub; this response still carries the
    # real content regardless, same convention as Phase 5's
    # TranscriptOut.is_retained.
    is_retained: bool


class GenerateNoteResponse(BaseModel):
    note: NoteOut
    degraded: bool


@router.post("/{session_id}/note")
async def generate_note(
    session_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> GenerateNoteResponse:
    """Triggers SOAP note generation for a session with a completed
    transcript (Phase 5). Degrades gracefully rather than erroring when
    the LLM fails — see app/services/note_service.py's DEGRADATION
    POLICY — so a 200 with degraded=true is a normal, expected outcome,
    not a bug.

    No manual tenant check on the session id: generate_note_for_session
    opens its DB work already scoped (via RLS) to the caller's practice
    — a session belonging to another practice is invisible, so it comes
    back 404, not 403, same as every other route in this API.
    """
    try:
        result = await generate_note_for_session(
            session_id=session_id,
            practice_id=user.practice_id,
            actor_user_id=user.id,
            request_meta=request_meta_from_request(request),
        )
    except SessionNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from None
    except TranscriptNotReadyError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No transcript available for this session yet"
        ) from None

    # Built from the service result's independent in-memory fields, NOT
    # from result.note's columns — those are gated empty when
    # is_retained is False (see NoteGenerationResult's docstring).
    return GenerateNoteResponse(
        note=NoteOut(
            id=result.note.id,
            status=result.note.status,
            subjective=result.subjective,
            objective=result.objective,
            assessment=result.assessment,
            plan=result.plan,
            full_text=result.full_text,
            provider=result.provider,
            model=result.model,
            prompt_version=result.note.prompt_version,
            version=result.note.version,
            degraded=result.degraded,
            is_retained=result.is_retained,
        ),
        degraded=result.degraded,
    )
