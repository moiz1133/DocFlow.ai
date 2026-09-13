"""GET /metrics (Phase 8) — app/api/metrics.py, app/ops/metrics.py,
app/ops/http_metrics.py.

CRITICAL boundary this file enforces: the exposition output must never
carry a patient/session/transcript/user identifier or free text as a
label value — see app/ops/metrics.py's module docstring for the exact
rule. A UUID appearing anywhere in the response is a hard failure.
"""

import re
import uuid

import pytest
from httpx import AsyncClient

from app.config import get_settings
from tests.helpers import auth_headers, register_owner

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


async def test_metrics_endpoint_exposes_expected_series(client: AsyncClient) -> None:
    # Generate some traffic first so the counters have at least one
    # series each to expose (a Counter/Histogram with zero observations
    # renders no lines at all under prometheus_client's default output).
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    await client.get("/v1/sessions/" + str(uuid.uuid4()), headers=headers)  # 404, still counted

    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text

    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    assert "http_in_flight_requests" in body
    assert "rate_limit_hits_total" in body
    assert "ws_active_connections" in body
    assert "transcription_latency_seconds" in body
    assert "note_generation_latency_seconds" in body
    assert "celery_task_total" in body
    assert "purge_rows_deleted_total" in body


async def test_metrics_output_is_phi_free_and_cardinality_safe(client: AsyncClient) -> None:
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    session_id = (await client.post("/v1/sessions", headers=headers)).json()["session_id"]
    await client.get(f"/v1/sessions/{session_id}", headers=headers)

    response = await client.get("/metrics")
    body = response.text

    # No UUID anywhere — not the session id just created, not the user's
    # id, not any other identifier that might have leaked into a label.
    assert not _UUID_RE.search(body), "metrics output must never contain a UUID"
    # The route label must be the TEMPLATE, never the raw path with the
    # session id substituted in.
    assert 'route="/v1/sessions/{session_id}"' in body
    assert f"/v1/sessions/{session_id}" not in body
    # No email, password, or transcript/note content field names as
    # VALUES (the metric/label *names* themselves are a fixed, known,
    # PHI-free vocabulary — see app/ops/metrics.py).
    assert owner["email"] not in body


async def test_metrics_route_template_used_not_raw_path_for_note_endpoint(
    client: AsyncClient,
) -> None:
    owner = await register_owner(client)
    headers = auth_headers(owner["access_token"])
    session_id = (await client.post("/v1/sessions", headers=headers)).json()["session_id"]
    await client.get(f"/v1/sessions/{session_id}/note", headers=headers)

    body = (await client.get("/metrics")).text
    assert 'route="/v1/sessions/{session_id}/note"' in body


async def test_metrics_endpoint_protected_when_auth_token_set(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected_settings = get_settings().model_copy(update={"METRICS_AUTH_TOKEN": "s3cr3t-token"})
    import app.api.metrics as metrics_module

    monkeypatch.setattr(metrics_module, "get_settings", lambda: protected_settings)

    unauthenticated = await client.get("/metrics")
    assert unauthenticated.status_code == 401

    wrong_token = await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert wrong_token.status_code == 401

    authenticated = await client.get("/metrics", headers={"Authorization": "Bearer s3cr3t-token"})
    assert authenticated.status_code == 200


async def test_metrics_endpoint_disabled_returns_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled_settings = get_settings().model_copy(update={"METRICS_ENABLED": False})
    import app.api.metrics as metrics_module

    monkeypatch.setattr(metrics_module, "get_settings", lambda: disabled_settings)

    response = await client.get("/metrics")
    assert response.status_code == 404
