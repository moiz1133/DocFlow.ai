"""WS /v1/sessions/{id}/stream — the live audio-streaming path.

Uses httpx-ws (see tests/conftest.py's ws_client fixture) rather than
starlette.testclient.TestClient so the whole flow runs on the same
event loop as the rest of the (session-scoped-loop) test suite.
"""

import ast
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.sessions as sessions_module
from app.auth.tokens import create_access_token, decode_access_token
from app.config import get_settings
from app.models import AuditLog
from app.transcription.mock import MockTranscriber
from tests.helpers import auth_headers, register_owner

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _create_session(client: AsyncClient, access_token: str) -> str:
    response = await client.post("/v1/sessions", headers=auth_headers(access_token))
    session_id: str = response.json()["session_id"]
    return session_id


def _ws_url(session_id: str, token: str) -> str:
    return f"/v1/sessions/{session_id}/stream?token={token}"


@asynccontextmanager
async def _connect(url: str, client: AsyncClient) -> AsyncIterator[AsyncWebSocketSession]:
    """Enters *and exits* the ws_client fixture's AsyncClient itself
    (see that fixture's docstring for why it's handed over un-entered),
    then connects the websocket — both within this one `async with`, so
    everything runs in the calling test's own task from start to finish.
    """
    ws: AsyncWebSocketSession
    async with client, aconnect_ws(url, client) as ws:
        yield ws


async def _receive_until_error(ws: AsyncWebSocketSession) -> dict[str, Any]:
    """Drains messages until an {"type": "error"} event arrives. Audio
    already queued before a limit is hit is still legitimately
    transcribed and relayed (transcript.partial/final) — the error isn't
    necessarily the very next message, just the eventual one.
    """
    while True:
        message = await ws.receive_json()
        if message["type"] == "error":
            return message  # type: ignore[no-any-return]


async def test_ws_happy_path_partial_then_final_then_complete(
    client: AsyncClient, ws_client: AsyncClient
) -> None:
    owner = await register_owner(client)
    access_token = owner["access_token"]
    session_id = await _create_session(client, access_token)

    async with _connect(_ws_url(session_id, access_token), ws_client) as ws:
        first = await ws.receive_json()
        assert first == {"type": "session.status", "status": "created"}

        await ws.send_json({"type": "start", "format": {"mime": "audio/wav", "sample_rate": 16000}})
        recording = await ws.receive_json()
        assert recording == {"type": "session.status", "status": "recording"}

        await ws.send_bytes(b"fake-audio-chunk-one")
        await ws.send_bytes(b"fake-audio-chunk-two")
        await ws.send_json({"type": "stop"})

        events = []
        while True:
            message = await ws.receive_json()
            events.append(message)
            if message == {"type": "session.status", "status": "complete"}:
                break

    partials = [e for e in events if e["type"] == "transcript.partial"]
    finals = [e for e in events if e["type"] == "transcript.final"]
    assert len(partials) > 0
    assert len(finals) > 0
    # Every final event carries at least one segment, even from
    # MockTranscriber (which never sets TranscriptionEvent.segment) — see
    # _send_transcript_event's fallback, mirroring StreamAggregate's.
    assert all(len(e["segments"]) > 0 for e in finals)
    assert events[-1] == {"type": "session.status", "status": "complete"}

    status_response = await client.get(
        f"/v1/sessions/{session_id}", headers=auth_headers(access_token)
    )
    body = status_response.json()
    assert body["status"] == "complete"
    assert body["transcript_exists"] is True


async def test_ws_missing_token_closes_unauthorized(ws_client: AsyncClient) -> None:
    async with _connect(f"/v1/sessions/{uuid.uuid4()}/stream", ws_client) as ws:
        error = await ws.receive_json()
        assert error["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4401


async def test_ws_garbage_token_closes_unauthorized(ws_client: AsyncClient) -> None:
    url = f"/v1/sessions/{uuid.uuid4()}/stream?token=not-a-real-token"
    async with _connect(url, ws_client) as ws:
        error = await ws.receive_json()
        assert error["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4401


async def test_ws_expired_token_closes_unauthorized(
    client: AsyncClient, ws_client: AsyncClient
) -> None:
    owner = await register_owner(client)
    claims = decode_access_token(owner["access_token"])
    expired_token = create_access_token(
        claims.user_id, claims.practice_id, claims.role, ttl_minutes=-1
    )

    url = _ws_url(str(uuid.uuid4()), expired_token)
    async with _connect(url, ws_client) as ws:
        error = await ws.receive_json()
        assert error["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4401


async def test_ws_cannot_stream_another_practices_session(
    client: AsyncClient, ws_client: AsyncClient
) -> None:
    owner_a = await register_owner(client)
    owner_b = await register_owner(client)
    session_id = await _create_session(client, owner_a["access_token"])

    # owner_b's token is perfectly valid — just for a different practice —
    # so this exercises the RLS-backed "not found" boundary, not auth.
    async with _connect(_ws_url(session_id, owner_b["access_token"]), ws_client) as ws:
        error = await ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "session_not_found"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4404


async def test_ws_unknown_session_closes_not_found(
    client: AsyncClient, ws_client: AsyncClient
) -> None:
    owner = await register_owner(client)
    async with _connect(_ws_url(str(uuid.uuid4()), owner["access_token"]), ws_client) as ws:
        await ws.receive_json()  # error event
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4404


async def test_ws_exceeding_max_audio_bytes_emits_error_and_closes(
    client: AsyncClient, ws_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiny_settings = get_settings().model_copy(update={"MAX_AUDIO_BYTES": 10})
    monkeypatch.setattr(sessions_module, "get_settings", lambda: tiny_settings)

    owner = await register_owner(client)
    session_id = await _create_session(client, owner["access_token"])

    async with _connect(_ws_url(session_id, owner["access_token"]), ws_client) as ws:
        await ws.receive_json()  # session.status: created
        await ws.send_json({"type": "start", "format": {"mime": "audio/wav", "sample_rate": 16000}})
        await ws.receive_json()  # session.status: recording

        await ws.send_bytes(b"x" * 100)  # well over the 10-byte cap

        error = await _receive_until_error(ws)
        assert error["code"] == "audio_bytes_exceeded"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4413

    status_response = await client.get(
        f"/v1/sessions/{session_id}", headers=auth_headers(owner["access_token"])
    )
    assert status_response.json()["status"] == "error"


async def test_ws_exceeding_max_session_seconds_emits_error_and_closes(
    client: AsyncClient, ws_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiny_settings = get_settings().model_copy(update={"MAX_SESSION_SECONDS": 0})
    monkeypatch.setattr(sessions_module, "get_settings", lambda: tiny_settings)

    owner = await register_owner(client)
    session_id = await _create_session(client, owner["access_token"])

    async with _connect(_ws_url(session_id, owner["access_token"]), ws_client) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "start", "format": {"mime": "audio/wav", "sample_rate": 16000}})
        await ws.receive_json()

        await ws.send_bytes(b"one-chunk-is-enough")

        error = await _receive_until_error(ws)
        assert error["code"] == "session_duration_exceeded"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 4413


async def test_ws_rate_limit_failure_emits_error_marks_error_and_audits(
    client: AsyncClient, ws_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sessions_module, "get_transcriber", lambda: MockTranscriber(simulate_rate_limit=True)
    )

    owner = await register_owner(client)
    session_id = await _create_session(client, owner["access_token"])

    async with _connect(_ws_url(session_id, owner["access_token"]), ws_client) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "start", "format": {"mime": "audio/wav", "sample_rate": 16000}})
        await ws.receive_json()

        await ws.send_bytes(b"some-audio")
        await ws.send_json({"type": "stop"})

        error = await ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "provider_rate_limited"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            await ws.receive_json()
    assert exc_info.value.code == 1011

    status_response = await client.get(
        f"/v1/sessions/{session_id}", headers=auth_headers(owner["access_token"])
    )
    assert status_response.json()["status"] == "error"


async def test_ws_failure_writes_an_audit_entry(
    client: AsyncClient,
    ws_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(
        sessions_module, "get_transcriber", lambda: MockTranscriber(simulate_rate_limit=True)
    )

    owner = await register_owner(client)
    session_id = await _create_session(client, owner["access_token"])

    async with _connect(_ws_url(session_id, owner["access_token"]), ws_client) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "start", "format": {"mime": "audio/wav", "sample_rate": 16000}})
        await ws.receive_json()
        await ws.send_bytes(b"some-audio")
        await ws.send_json({"type": "stop"})
        with pytest.raises(WebSocketDisconnect):
            while True:
                await ws.receive_json()

    async with admin_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "session",
                AuditLog.resource_id == uuid.UUID(session_id),
                AuditLog.action == "update",
            )
        )
        rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].metadata_ == {"status": "error", "reason": "provider_rate_limited"}


async def test_ws_zero_retention_no_file_writes_during_streaming(
    client: AsyncClient, ws_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No point in the WS request lifecycle opens a file for writing — the
    only place audio is ever handed off is into the bounded in-memory
    queue that feeds the transcriber (see
    app/services/transcription_service.py's NEVER-PERSIST comments).
    """
    import builtins

    real_open = builtins.open
    write_mode_calls: list[str] = []

    def _spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in ("w", "a", "x")):
            write_mode_calls.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy_open)

    owner = await register_owner(client)
    session_id = await _create_session(client, owner["access_token"])

    async with _connect(_ws_url(session_id, owner["access_token"]), ws_client) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "start", "format": {"mime": "audio/wav", "sample_rate": 16000}})
        await ws.receive_json()
        await ws.send_bytes(b"fake-audio-chunk-one")
        await ws.send_bytes(b"fake-audio-chunk-two")
        await ws.send_json({"type": "stop"})

        while True:
            message = await ws.receive_json()
            if message == {"type": "session.status", "status": "complete"}:
                break

    assert write_mode_calls == []


def _module_source_has_no_disk_or_object_storage_calls(path: Path) -> list[str]:
    """AST-based guardrail (same technique as
    tests/test_transcription_isolation.py): confirms the audio-handling
    modules never reference filesystem-write or object-storage APIs by
    name, as a static complement to the dynamic open()-spy test above.
    """
    banned_names = {"open", "boto3", "S3", "write_bytes", "write_text"}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in banned_names:
            offenders.append(name)
    return offenders


def test_zero_retention_modules_never_reference_storage_apis() -> None:
    for relative_path in (
        "app/api/sessions.py",
        "app/services/transcription_service.py",
    ):
        offenders = _module_source_has_no_disk_or_object_storage_calls(REPO_ROOT / relative_path)
        assert offenders == [], f"{relative_path} references storage APIs: {offenders}"
