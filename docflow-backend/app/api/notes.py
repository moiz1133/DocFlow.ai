"""SOAP note generation endpoints, mounted at /v1/sessions (extends the
Phase 5 session resource with one more sub-resource: its generated note).

Phase 8: note generation moved off the request thread onto a Celery
worker (app/worker/tasks.py) — POST enqueues and returns 202
immediately; GET polls for the result. See that module's docstring for
why app/services/note_service.py's generate_note_for_session needed no
changes to make this possible.

BEHAVIOR CHANGE from Phase 6: the synchronous POST response used to
carry the real, in-memory note/transcript content regardless of
is_retained (the Phase 7 consent gate only ever governed what got
WRITTEN to the DB, never what the caller who just triggered generation
was told). That in-memory content no longer has anywhere to live once
generation moves to a separate worker process — a poller finds out what
happened by reading GET .../note, which can only ever return what
actually made it into the notes table. So when a session's content is
not retained, GET .../note now legitimately returns empty sections
(is_retained=False, subjective/objective/.../full_text all None) even
on a successful, non-degraded generation. This is an inherent
consequence of async processing plus zero/consented retention, not a
regression — there is no other honest answer once the content that
would have been returned was, by policy, never persisted anywhere this
process can still read it back from.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_tenant_session
from app.models import EncounterSession, Note, Transcript, User
from app.models.enums import NoteStatus, SessionStatus
from app.ops.ratelimit import UserRateLimiter
from app.services.audit_service import request_meta_from_request
from app.services.transcription_service import tenant_session
from app.worker.tasks import generate_note_task

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
    # DEGRADATION POLICY. None when is_retained is False — see the module
    # docstring's BEHAVIOR CHANGE note.
    full_text: str | None
    provider: str | None
    model: str | None
    prompt_version: str | None
    version: int
    degraded: bool
    # Whether this content was actually written to the notes table (the
    # Phase 7 consent gate — app/security/consent.py).
    is_retained: bool


class GenerateNoteAcceptedResponse(BaseModel):
    task_id: str
    status: Literal["generating"] = "generating"


class NoteStatusResponse(BaseModel):
    """GET .../note's shape: `status` mirrors the session's own lifecycle
    (see app/models/enums.py's SessionStatus) so a poller can tell
    "still working" apart from "done" without a separate enum. `note` is
    populated only once the session has reached a note-generation
    terminal state (complete or complete_degraded).
    """

    status: SessionStatus
    note: NoteOut | None


def _note_out(note: Note) -> NoteOut:
    return NoteOut(
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
        is_retained=note.is_retained,
    )


@router.post(
    "/{session_id}/note",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(UserRateLimiter("note_generate"))],
)
async def generate_note(
    session_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> GenerateNoteAcceptedResponse:
    """Validates the session is ready (exists, has a transcript), marks
    it `generating`, and enqueues app/worker/tasks.py's
    generate_note_task — the actual generation happens on a worker, not
    this request thread. Poll GET .../note for the result.

    No manual tenant check on the session id: the lookup below is
    already RLS-scoped to the caller's practice — a session belonging to
    another practice is invisible, so it comes back 404, not 403, same
    as every other route in this API.
    """
    async with tenant_session(user.practice_id) as db:
        encounter = await db.get(EncounterSession, session_id)
        if encounter is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

        transcript_exists = await db.execute(
            select(Transcript.id).where(Transcript.session_id == session_id)
        )
        if transcript_exists.first() is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "No transcript available for this session yet"
            )

        encounter.status = SessionStatus.generating
        await db.flush()

    task = generate_note_task.delay(
        str(session_id),
        str(user.practice_id),
        str(user.id),
        request_meta_from_request(request),
    )
    return GenerateNoteAcceptedResponse(task_id=task.id)


@router.get("/{session_id}/note", dependencies=[Depends(UserRateLimiter("read"))])
async def get_note_status(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> NoteStatusResponse:
    encounter = await db.get(EncounterSession, session_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    note_row: Note | None = None
    if encounter.status in (SessionStatus.complete, SessionStatus.complete_degraded):
        result = await db.execute(
            select(Note).where(Note.session_id == session_id).order_by(Note.version.desc())
        )
        note_row = result.scalars().first()

    return NoteStatusResponse(
        status=encounter.status, note=_note_out(note_row) if note_row is not None else None
    )
