"""POST /v1/sessions/{id}/finalize — the non-streaming audio-in fallback,
and retention gating, which is easiest to exercise through this simpler
synchronous path (it shares app.services.transcription_service.
persist_transcript with the WS path, so what's verified here applies
identically there).
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import decode_access_token
from app.models import AuditLog, Consent, Transcript
from app.models.enums import ConsentType
from tests.helpers import auth_headers, register_owner


async def _create_session(client: AsyncClient, access_token: str) -> str:
    response = await client.post("/v1/sessions", headers=auth_headers(access_token))
    session_id: str = response.json()["session_id"]
    return session_id


async def _fetch_transcript(
    admin_sessionmaker: async_sessionmaker[AsyncSession], session_id: str
) -> Transcript:
    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(Transcript).where(Transcript.session_id == uuid.UUID(session_id))
        )
        transcript = result.scalar_one_or_none()
        assert transcript is not None, "expected a Transcript row to exist"
        return transcript


async def _grant_retention_consent(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    *,
    practice_id: uuid.UUID,
    session_id: str,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        session.add(
            Consent(
                practice_id=practice_id,
                session_id=uuid.UUID(session_id),
                consent_type=ConsentType.retention,
                granted=True,
                granted_by="test-harness",
            )
        )


async def test_finalize_transcribes_and_persists(client: AsyncClient) -> None:
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    session_id = await _create_session(client, owner["access_token"])

    files = {"audio": ("session.wav", b"fake-recorded-audio-bytes", "audio/wav")}
    response = await client.post(
        f"/v1/sessions/{session_id}/finalize", headers=headers, files=files
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript"]["content"]  # mock fixture text, non-empty
    assert len(body["transcript"]["segments"]) > 0
    assert body["transcript"]["segments"][0]["speaker"] is None

    status_response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    status_body = status_response.json()
    assert status_body["status"] == "complete"
    assert status_body["transcript_exists"] is True


async def test_finalize_rejects_empty_upload(client: AsyncClient) -> None:
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    session_id = await _create_session(client, owner["access_token"])

    files = {"audio": ("session.wav", b"", "audio/wav")}
    response = await client.post(
        f"/v1/sessions/{session_id}/finalize", headers=headers, files=files
    )

    assert response.status_code == 400


async def test_finalize_404_for_unknown_session(client: AsyncClient) -> None:
    owner = await register_owner(client)
    files = {"audio": ("session.wav", b"some-bytes", "audio/wav")}
    response = await client.post(
        f"/v1/sessions/{uuid.uuid4()}/finalize",
        headers=auth_headers(owner["access_token"]),
        files=files,
    )
    assert response.status_code == 404


async def test_finalize_without_consent_defaults_to_not_retained(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """TRANSCRIPT_RETENTION_DEFAULT=consented (this suite's default, see
    Settings) with no Consent on file: the caller still gets the real
    transcript back in the HTTP response, but nothing is stored.
    """
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    session_id = await _create_session(client, owner["access_token"])

    files = {"audio": ("session.wav", b"fake-recorded-audio-bytes", "audio/wav")}
    response = await client.post(
        f"/v1/sessions/{session_id}/finalize", headers=headers, files=files
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript"]["is_retained"] is False
    assert body["transcript"]["content"]  # caller still gets it in-memory

    stored = await _fetch_transcript(admin_sessionmaker, session_id)
    assert stored.is_retained is False
    assert stored.content == ""
    assert stored.segments == []


async def test_finalize_with_consent_is_retained(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner = await register_owner(client)
    access_token = owner["access_token"]
    headers = auth_headers(access_token)
    session_id = await _create_session(client, access_token)
    practice_id = decode_access_token(access_token).practice_id

    await _grant_retention_consent(
        admin_sessionmaker, practice_id=practice_id, session_id=session_id
    )

    files = {"audio": ("session.wav", b"fake-recorded-audio-bytes", "audio/wav")}
    response = await client.post(
        f"/v1/sessions/{session_id}/finalize", headers=headers, files=files
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transcript"]["is_retained"] is True

    stored = await _fetch_transcript(admin_sessionmaker, session_id)
    assert stored.is_retained is True
    assert stored.content == body["transcript"]["content"]
    assert len(stored.segments) == len(body["transcript"]["segments"])


async def test_finalize_writes_transcript_created_audit_entry(
    client: AsyncClient, admin_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    session_id = await _create_session(client, owner["access_token"])

    files = {"audio": ("session.wav", b"fake-recorded-audio-bytes", "audio/wav")}
    await client.post(f"/v1/sessions/{session_id}/finalize", headers=headers, files=files)

    transcript = await _fetch_transcript(admin_sessionmaker, session_id)
    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "transcript",
                AuditLog.resource_id == transcript.id,
                AuditLog.action == "create",
            )
        )
        audit_rows = result.scalars().all()

    # Also expect a retention_skipped decision audit alongside this one
    # (Phase 7's consent gate — no consent exists in this test) — scoped
    # out here since this assertion is specifically about transcript.created.
    assert len(audit_rows) == 1
    assert audit_rows[0].action.value == "create"
    assert audit_rows[0].metadata_ is not None
    assert audit_rows[0].metadata_["provider"] == "mock"
    # PHI-free: only counts/provider metadata, never transcript text.
    assert "content" not in audit_rows[0].metadata_
    assert "text" not in audit_rows[0].metadata_
