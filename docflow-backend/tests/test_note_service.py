"""End-to-end tests for POST /v1/sessions/{id}/note — happy path, retry,
degradation, scrubbing, and auth/tenant enforcement. Exercises the real
route (app/api/notes.py) -> service (app/services/note_service.py) ->
NoteGenerator interface path, with the generator itself swapped for
test doubles via monkeypatching app.services.note_service's imported
name, the same technique tests/test_sessions_ws.py uses for the
transcriber.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.note_service as note_service_module
from app.auth.tokens import decode_access_token
from app.config import get_settings
from app.models import AuditLog, EncounterSession, Transcript
from app.models.enums import SessionStatus
from app.notes.base import NoteContext, NoteGenerator, SoapNote
from app.notes.mock import MockNoteGenerator
from tests.helpers import auth_headers, register_owner

_CHATTER_TRANSCRIPT = (
    "Good morning, how's the family doing? "
    "I've had a mild headache and some fatigue for about three days now. "
    "Nice weather we're having. "
    "No fever that I've noticed, no nausea."
)


class _SpyNoteGenerator(NoteGenerator):
    """Wraps MockNoteGenerator but records exactly what transcript text it
    was handed, so tests can assert on what the scrubbing pre-step
    actually produced without depending on MockNoteGenerator's own
    (content-independent) fixture output.
    """

    provider_name = "mock"

    def __init__(self) -> None:
        self.received_text: str | None = None
        self._delegate = MockNoteGenerator()

    async def generate_soap(self, transcript_text: str, context: NoteContext) -> SoapNote:
        self.received_text = transcript_text
        note = await self._delegate.generate_soap(transcript_text, context)
        self.last_retry_count = self._delegate.last_retry_count
        return note


async def _seed_completed_session(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    clinician_id: uuid.UUID,
    transcript_text: str,
    is_retained: bool = True,
) -> uuid.UUID:
    async with admin_sessionmaker() as session, session.begin():
        encounter = EncounterSession(
            practice_id=practice_id, clinician_id=clinician_id, status=SessionStatus.complete
        )
        session.add(encounter)
        await session.flush()
        session.add(
            Transcript(
                practice_id=practice_id,
                session_id=encounter.id,
                content=transcript_text,
                segments=[{"speaker": None, "start": None, "end": None, "text": transcript_text}],
                is_retained=is_retained,
            )
        )
        return encounter.id


async def _owner_ids(client: AsyncClient) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    """Registers an owner; returns (auth headers, practice_id, user_id)."""
    owner = await register_owner(client)
    claims = decode_access_token(owner["access_token"])
    return auth_headers(owner["access_token"]), claims.practice_id, claims.user_id


async def test_happy_path_generates_a_valid_note_and_completes_session(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is False
    note = body["note"]
    assert note["status"] == "draft"
    assert note["subjective"].strip()
    assert note["objective"].strip()
    assert note["assessment"].strip()
    assert note["plan"].strip()
    assert note["degraded"] is False
    assert note["provider"] == "mock"
    assert note["prompt_version"] == "soap_primary_care_v1"

    status_response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    assert status_response.json()["status"] == "complete"

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "note", AuditLog.resource_id == uuid.UUID(note["id"])
            )
        )
        audit_rows = result.scalars().all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action.value == "create"
    assert audit_rows[0].metadata_ == {
        "provider": "mock",
        "model": None,
        "prompt_version": "soap_primary_care_v1",
        "retry_count": 0,
        "degraded": False,
    }


async def test_retry_succeeds_and_records_retry_count(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        note_service_module,
        "get_note_generator",
        lambda: MockNoteGenerator(simulate_malformed_once=True),
    )

    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is False
    note_id = uuid.UUID(body["note"]["id"])

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "note",
                AuditLog.resource_id == note_id,
                AuditLog.action == "create",
            )
        )
        audit_row = result.scalars().one()
    assert audit_row.metadata_ is not None
    assert audit_row.metadata_["retry_count"] == 1


async def test_degradation_returns_transcript_and_marks_session_degraded(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript_text = "I've had a mild headache and some fatigue for about three days now."
    monkeypatch.setattr(
        note_service_module,
        "get_note_generator",
        lambda: MockNoteGenerator(simulate_always_malformed=True),
    )

    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text=transcript_text,
    )

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is True
    note = body["note"]
    assert note["degraded"] is True
    assert note["subjective"] is None
    assert note["objective"] is None
    assert note["assessment"] is None
    assert note["plan"] is None
    # The visit is never lost: the raw transcript comes back as full_text.
    assert note["full_text"] == transcript_text

    status_response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    assert status_response.json()["status"] == "complete_degraded"

    note_id = uuid.UUID(note["id"])
    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "note",
                AuditLog.resource_id == note_id,
                AuditLog.action == "note_degraded",
            )
        )
        audit_row = result.scalars().one()
    assert audit_row.metadata_ is not None
    assert audit_row.metadata_["degraded"] is True
    assert audit_row.metadata_["reason"] == "NoteValidationError"


async def test_scrubbing_removes_chatter_before_reaching_the_generator_and_leaves_transcript_intact(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _SpyNoteGenerator()
    monkeypatch.setattr(note_service_module, "get_note_generator", lambda: spy)

    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text=_CHATTER_TRANSCRIPT,
    )

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert response.status_code == 200, response.text

    assert spy.received_text is not None
    assert "how's the family" not in spy.received_text.lower()
    assert "nice weather" not in spy.received_text.lower()
    assert "mild headache" in spy.received_text.lower()

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(Transcript).where(Transcript.session_id == session_id)
        )
        stored = result.scalar_one()
    assert stored.content == _CHATTER_TRANSCRIPT  # untouched by scrubbing


async def test_note_scrub_disabled_setting_skips_scrubbing(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _SpyNoteGenerator()
    monkeypatch.setattr(note_service_module, "get_note_generator", lambda: spy)
    disabled_settings = get_settings().model_copy(update={"NOTE_SCRUB_ENABLED": False})
    monkeypatch.setattr(note_service_module, "get_settings", lambda: disabled_settings)

    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text=_CHATTER_TRANSCRIPT,
    )

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert response.status_code == 200, response.text

    assert spy.received_text == _CHATTER_TRANSCRIPT  # chatter still present, unscrubbed


async def test_cannot_generate_note_for_another_practices_session(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _headers_a, practice_id_a, user_id_a = await _owner_ids(client)
    headers_b, _practice_id_b, _user_id_b = await _owner_ids(client)

    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id_a,
        clinician_id=user_id_a,
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers_b)
    assert response.status_code == 404


async def test_404_for_unknown_session(client: AsyncClient) -> None:
    headers, _practice_id, _user_id = await _owner_ids(client)
    response = await client.post(f"/v1/sessions/{uuid.uuid4()}/note", headers=headers)
    assert response.status_code == 404


async def test_409_when_session_has_no_transcript_yet(client: AsyncClient) -> None:
    headers, _practice_id, _user_id = await _owner_ids(client)
    create_response = await client.post("/v1/sessions", headers=headers)
    session_id = create_response.json()["session_id"]

    response = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert response.status_code == 409
