"""Prometheus collectors (Phase 8) — the metric objects themselves.
GET /metrics (app/api/metrics.py) renders whatever is registered here.

CRITICAL — read before adding a metric or a label: every series in this
module must stay PHI-free and cardinality-safe. Allowed labels are
closed sets known in advance (route TEMPLATE — never a raw path with an
id in it —, HTTP method, status code, vendor/provider name, task name,
outcome). NEVER a patient/session/transcript/user identifier or any
free text; those are unbounded-cardinality and would also leak PHI-
adjacent identifiers into a system (Prometheus) that has none of
app/security/'s controls. tests/test_metrics.py asserts the exposition
output never contains a UUID or the word "transcript"/"note" as a
label VALUE (only as fixed label keys, e.g. resource_type="purge", is
fine — see that test for the exact boundary).

A dedicated CollectorRegistry (not prometheus_client's global default)
so importing this module in tests repeatedly (module reload across
test files) never raises "Duplicated timeseries" the way registering
onto the process-wide default registry would.
"""

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

# --- HTTP ---------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    ["route", "method", "status"],
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["route", "method"],
    registry=REGISTRY,
)
HTTP_IN_FLIGHT_REQUESTS = Gauge(
    "http_in_flight_requests",
    "HTTP requests currently being handled",
    registry=REGISTRY,
)

# --- Rate limiting --------------------------------------------------------

RATE_LIMIT_HITS_TOTAL = Counter(
    "rate_limit_hits_total",
    "Requests rejected by the rate limiter, by bucket",
    ["bucket"],
    registry=REGISTRY,
)

# --- WebSocket audio streaming --------------------------------------------

WS_ACTIVE_CONNECTIONS = Gauge(
    "ws_active_connections",
    "Currently open /stream WebSocket connections",
    registry=REGISTRY,
)
WS_SESSION_DURATION_SECONDS = Histogram(
    "ws_session_duration_seconds",
    "Duration of a /stream WebSocket connection, from accept to close",
    ["outcome"],
    registry=REGISTRY,
)

# --- Vendor calls (transcription, note generation) ------------------------

TRANSCRIPTION_LATENCY_SECONDS = Histogram(
    "transcription_latency_seconds",
    "Transcription vendor call latency",
    ["provider", "outcome"],
    registry=REGISTRY,
)
NOTE_GENERATION_LATENCY_SECONDS = Histogram(
    "note_generation_latency_seconds",
    "Note generation vendor call latency",
    ["provider", "outcome"],
    registry=REGISTRY,
)

# --- Celery tasks -----------------------------------------------------------

CELERY_TASK_TOTAL = Counter(
    "celery_task_total",
    "Celery task invocations, by task name and outcome",
    ["task", "outcome"],
    registry=REGISTRY,
)
CELERY_TASK_DURATION_SECONDS = Histogram(
    "celery_task_duration_seconds",
    "Celery task latency",
    ["task"],
    registry=REGISTRY,
)

# --- Retention purge job ----------------------------------------------------

PURGE_ROWS_DELETED_TOTAL = Counter(
    "purge_rows_deleted_total",
    "Rows deleted by the retention purge job, by resource type",
    ["resource_type"],
    registry=REGISTRY,
)

__all__ = [
    "CELERY_TASK_DURATION_SECONDS",
    "CELERY_TASK_TOTAL",
    "CONTENT_TYPE_LATEST",
    "HTTP_IN_FLIGHT_REQUESTS",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "NOTE_GENERATION_LATENCY_SECONDS",
    "PURGE_ROWS_DELETED_TOTAL",
    "RATE_LIMIT_HITS_TOTAL",
    "REGISTRY",
    "TRANSCRIPTION_LATENCY_SECONDS",
    "WS_ACTIVE_CONNECTIONS",
    "WS_SESSION_DURATION_SECONDS",
]
