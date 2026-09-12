"""SOAP note generation endpoint, mounted at /v1/sessions (extends the
Phase 5 session resource with one more sub-resource: its generated note).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.models import User
from app.models.enums import NoteStatus
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
    # readable here.
    full_text: str | None
    provider: str | None
    model: str | None
    prompt_version: str | None
    version: int
    degraded: bool


class GenerateNoteResponse(BaseModel):
    note: NoteOut
    degraded: bool


@router.post("/{session_id}/note")
async def generate_note(
    session_id: uuid.UUID,
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
            session_id=session_id, practice_id=user.practice_id, actor_user_id=user.id
        )
    except SessionNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from None
    except TranscriptNotReadyError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No transcript available for this session yet"
        ) from None

    note = result.note
    return GenerateNoteResponse(
        note=NoteOut(
            id=note.id,
            status=note.status,
            subjective=note.subjective,
            objective=note.objective,
            assessment=note.assessment,
            plan=note.plan,
            full_text=note.full_text,
            provider=note.provider,
            model=note.model,
            prompt_version=note.prompt_version,
            version=note.version,
            degraded=note.degraded,
        ),
        degraded=result.degraded,
    )
