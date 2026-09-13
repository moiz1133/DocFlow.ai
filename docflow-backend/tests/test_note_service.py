"""End-to-end tests for POST/GET /v1/sessions/{id}/note — happy path,
retry, degradation, scrubbing, auth/tenant enforcement, and the Phase 8
async enqueue/poll flow. Exercises the real route (app/api/notes.py) ->
Celery task (app/worker/tasks.py, run eagerly — see
tests/conftest.py's _celery_eager_mode) -> service
(app/services/note_service.py) -> NoteGenerator interface path, with the
generator itself swapped for test doubles via monkeypatching
app.services.note_service's imported name, the same technique
tests/test_sessions_ws.py uses for the transcriber.

Because Celery runs tasks eagerly (synchronously, inline) in this test
suite, POST .../note has already fully completed generation by the time
it returns 202 — a real deployment's client would still need to poll
GET .../note, and every test below still exercises that route, but the
result is available immediately rather than needing an actual wait.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.note_service as note_service_module
from app.auth.tokens import decode_access_token
from app.config import get_settings
from app.models import AuditLog, Consent, EncounterSession, Note, Practice, Transcript, User
from app.models.enums import ConsentType, SessionStatus, UserRole
from app.notes.base import NoteContext, NoteGenerator, SoapNote
from app.notes.mock import MockNoteGenerator
from app.worker.tasks import generate_note_task
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


async def _grant_retention_consent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    """Makes ConsentService.assert_retention_allowed return True for this
    session under the default TRANSCRIPT_RETENTION_DEFAULT="consented"
    policy — see app/security/consent.py.
    """
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            Consent(
                practice_id=practice_id,
                session_id=session_id,
                consent_type=ConsentType.retention,
                granted=True,
                granted_by="test",
            )
        )


async def _owner_ids(client: AsyncClient) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
    """Registers an owner; returns (auth headers, practice_id, user_id)."""
    owner = await register_owner(client)
    claims = decode_access_token(owner["access_token"])
    return auth_headers(owner["access_token"]), claims.practice_id, claims.user_id


async def _generate_and_poll(
    client: AsyncClient, session_id: uuid.UUID, headers: dict[str, str]
) -> Any:
    """POSTs to enqueue, asserts the 202 contract, then GETs the result.
    Eager Celery means the task has already run by the time POST
    returns, but every caller still goes through the real poll route —
    see the module docstring.
    """
    accepted = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert accepted.status_code == 202, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["status"] == "generating"
    assert accepted_body["task_id"]

    polled = await client.get(f"/v1/sessions/{session_id}/note", headers=headers)
    assert polled.status_code == 200, polled.text
    return polled.json()


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

    body = await _generate_and_poll(client, session_id, headers)

    assert body["status"] == "complete"
    note = body["note"]
    assert note is not None
    assert note["status"] == "draft"
    assert note["degraded"] is False
    assert note["provider"] == "mock"
    assert note["prompt_version"] == "soap_primary_care_v1"
    # No retention consent granted — see the module docstring's note on
    # the Phase 8 behavior change: an un-retained note's sections are
    # genuinely empty by the time a poller reads them back.
    assert note["is_retained"] is False
    assert note["subjective"] is None

    status_response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    assert status_response.json()["status"] == "complete"

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "note",
                AuditLog.resource_id == uuid.UUID(note["id"]),
                AuditLog.action == "create",
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
        "is_retained": False,
    }


async def test_happy_path_with_retention_consent_returns_full_note(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """With retention consent granted, GET .../note can actually return
    the generated content — the complement to the test above.
    """
    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )
    await _grant_retention_consent(
        admin_sessionmaker, practice_id=practice_id, session_id=session_id
    )

    body = await _generate_and_poll(client, session_id, headers)

    note = body["note"]
    assert note is not None
    assert note["is_retained"] is True
    assert note["subjective"].strip()
    assert note["objective"].strip()
    assert note["assessment"].strip()
    assert note["plan"].strip()


async def test_note_generation_audits_the_transcript_read(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """HIPAA requires auditing PHI *reads*, not just writes (Phase 7) —
    generate_note_for_session reads the stored transcript before ever
    calling the note generator, and that read must be audited exactly
    like a write is, with PHI-free metadata and ip/user-agent captured
    from the enqueueing request.
    """
    headers, practice_id, user_id = await _owner_ids(client)
    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=user_id,
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )

    accepted = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert accepted.status_code == 202, accepted.text

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(Transcript.id).where(Transcript.session_id == session_id)
        )
        transcript_id = result.scalar_one()

        audit_result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "transcript",
                AuditLog.resource_id == transcript_id,
                AuditLog.action == "read",
            )
        )
        read_rows = audit_result.scalars().all()

    assert len(read_rows) == 1
    assert read_rows[0].metadata_ == {"purpose": "note_generation"}
    assert read_rows[0].actor_user_id == user_id
    # PHI-free: no transcript text anywhere in the audit metadata.
    assert "headache" not in str(read_rows[0].metadata_)


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

    body = await _generate_and_poll(client, session_id, headers)

    note = body["note"]
    assert note is not None
    assert note["degraded"] is False
    note_id = uuid.UUID(note["id"])

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


async def test_degradation_marks_session_degraded_and_persists_transcript_when_retained(
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
    await _grant_retention_consent(
        admin_sessionmaker, practice_id=practice_id, session_id=session_id
    )

    body = await _generate_and_poll(client, session_id, headers)

    assert body["status"] == "complete_degraded"
    note = body["note"]
    assert note is not None
    assert note["degraded"] is True
    assert note["subjective"] is None
    assert note["objective"] is None
    assert note["assessment"] is None
    assert note["plan"] is None
    # The visit is never lost: with retention consent granted, the raw
    # transcript comes back as full_text even on a poll after the fact.
    assert note["full_text"] == transcript_text

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


async def test_degradation_without_retention_consent_leaves_note_empty_on_poll(
    client: AsyncClient,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complement to the test above: without consent, even the
    degradation stub's full_text (normally the raw transcript) is empty
    once read back through GET .../note — see the module docstring's
    Phase 8 behavior-change note.
    """
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
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )

    body = await _generate_and_poll(client, session_id, headers)

    assert body["status"] == "complete_degraded"
    note = body["note"]
    assert note is not None
    assert note["degraded"] is True
    assert note["is_retained"] is False
    assert note["full_text"] is None


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

    accepted = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert accepted.status_code == 202, accepted.text

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

    accepted = await client.post(f"/v1/sessions/{session_id}/note", headers=headers)
    assert accepted.status_code == 202, accepted.text

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

    poll_response = await client.get(f"/v1/sessions/{session_id}/note", headers=headers_b)
    assert poll_response.status_code == 404


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


async def test_poll_before_generation_returns_session_status_with_no_note(
    client: AsyncClient,
) -> None:
    headers, _practice_id, _user_id = await _owner_ids(client)
    create_response = await client.post("/v1/sessions", headers=headers)
    session_id = create_response.json()["session_id"]

    response = await client.get(f"/v1/sessions/{session_id}/note", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "created"
    assert body["note"] is None


async def test_generate_note_task_is_idempotent_on_double_run(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Calling generate_note_task twice for the same session (simulating
    a Celery-level retry after the task otherwise completed) must not
    create a second Note row — see app/worker/tasks.py's idempotency
    guard.
    """
    suffix = uuid.uuid4().hex[:8]
    async with admin_sessionmaker() as session, session.begin():
        practice = Practice(name=f"Idempotency Test {suffix}")
        session.add(practice)
        await session.flush()
        clinician = User(
            practice_id=practice.id,
            email=f"idempotency-{suffix}@example.com",
            full_name="Idempotency Clinician",
            role=UserRole.clinician,
        )
        session.add(clinician)
        await session.flush()
        practice_id, clinician_id = practice.id, clinician.id

    session_id = await _seed_completed_session(
        admin_sessionmaker,
        practice_id=practice_id,
        clinician_id=clinician_id,
        transcript_text="I've had a mild headache and some fatigue for about three days now.",
    )

    first = generate_note_task.delay(str(session_id), str(practice_id), str(clinician_id), None)
    first_result = first.get()
    assert first_result["outcome"] == "success"

    second = generate_note_task.delay(str(session_id), str(practice_id), str(clinician_id), None)
    second_result = second.get()
    assert second_result["outcome"] == "skipped_duplicate"

    async with admin_sessionmaker() as session:
        result = await session.execute(select(Note.id).where(Note.session_id == session_id))
        note_ids = result.scalars().all()
    assert len(note_ids) == 1
