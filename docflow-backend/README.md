# docflow-backend

Backend for DocFlow.ai, an ambient AI medical scribe.

- **Phase 1** (done): project skeleton — config, structured/PHI-safe
  logging, health endpoints, dev/deploy tooling.
- **Phase 2** (done): data layer — SQLAlchemy 2.0 models, Alembic
  migrations, practice-scoped multi-tenancy enforced via Postgres
  Row-Level Security.
- **Phase 3** (done): stateless JWT auth (access + refresh, MFA-ready,
  role-based access), per-request RLS wiring.
- **Phase 4** (done): vendor-agnostic transcription interface
  (`app/transcription/`) with a mock and an OpenAI implementation,
  selected by env. Not wired to any route yet.
- **Phase 5** (done): audio ingestion — a WebSocket streaming endpoint and
  a non-streaming finalize fallback (`app/api/sessions.py`), wiring the
  Phase 4 `Transcriber` interface to real sessions and persisting
  transcripts, with zero-retention audio handling and consent-gated
  retention. Ends at a stored transcript.
- **Phase 6** (done): SOAP note generation — a vendor-agnostic
  `NoteGenerator` interface (`app/notes/`) with a mock and an OpenAI
  implementation, a conservative chatter-scrubbing pre-step, schema
  validation with retry-then-degrade, and `POST /v1/sessions/{id}/note`
  (`app/services/note_service.py`) wiring it to a Phase 5 transcript.
- **Phase 7** (done): the HIPAA control layer — field-level AES-256
  envelope encryption for PHI columns (`app/security/`), TLS enforcement
  and security headers in transit, PHI *read* access auditing (not just
  writes), a centralized consent gate before any retention, and a
  fail-fast startup self-check that refuses to boot a misconfigured
  `PHI_MODE=real` deployment. Hardens Phases 1-6; no new product surface.
- **Phase 8** (done): hardening and operations — a central Redis-backed
  rate limiter (`app/ops/ratelimit.py`) across auth, session, and note
  routes plus WS connection establishment; PHI-free, cardinality-safe
  Prometheus metrics (`GET /metrics`); a real self-hosted-only Langfuse
  integration for note-generation tracing (`app/ops/tracing.py`); SOAP
  note generation moved off the request thread onto a Celery worker
  (`app/worker/`), with `POST /v1/sessions/{id}/note` now enqueuing and
  `GET /v1/sessions/{id}/note` polling; and a scheduled retention-purge
  job that makes zero/consented retention true over time, not just at
  write time. Hardens/operationalizes Phases 1-7; no new product surface.

Full business routes (note editing/finalization, patient-facing views)
land in later phases.

## Stack

- Python 3.12, FastAPI, Uvicorn
- `uv` for dependency management
- `pydantic-settings` for config
- Structured JSON logging with PHI + auth-secret redaction
- SQLAlchemy 2.0 (async, `Mapped[]` style) + Alembic, asyncpg driver
- PyJWT, argon2-cffi (password hashing), pyotp (TOTP/MFA)
- OpenAI SDK (transcription + note-generation vendor — isolated behind
  `app/transcription/` and `app/notes/` respectively)
- WebSockets (FastAPI/Starlette native) for live audio streaming
- `cryptography` (AES-256-GCM envelope encryption — `app/security/`);
  `boto3` optional/lazy, only for the AWS KMS key provider in production
- Celery (Redis broker/result backend) for async note generation and the
  scheduled retention-purge job (`app/worker/`)
- `prometheus-client` for `GET /metrics`; `langfuse` (self-hosted only,
  lazily imported — `app/ops/tracing.py`) for LLM tracing
- Ruff, mypy, pytest for dev tooling; `httpx-ws` for WS integration tests
- Docker + docker-compose (app, worker, beat, PostgreSQL 16, Redis 7)

## Local development

```bash
cp .env.example .env
make install
make db-up        # starts Postgres via docker-compose
make migrate-up    # alembic upgrade head (also creates the app DB role)
make seed          # one practice + one owner user, synthetic data only
make run
```

The API is served at `http://localhost:8000`. Liveness: `GET /health`.
Readiness (checks Postgres + Redis): `GET /health/ready`.

## Common tasks

| Command           | Description                                        |
|--------------------|-----------------------------------------------------|
| `make install`     | Install dependencies via uv                          |
| `make run`         | Run the dev server with reload                       |
| `make db-up`       | Start Postgres via docker-compose, wait until healthy|
| `make redis-up`    | Start Redis via docker-compose, wait until healthy   |
| `make migrate-up`  | Apply all Alembic migrations (`alembic upgrade head`)|
| `make migrate-down`| Roll back all migrations (`alembic downgrade base`)  |
| `make seed`        | Insert one dev practice + owner user (synthetic)     |
| `make test`        | Run the test suite with coverage (starts Postgres + Redis first) |
| `make lint`        | Run Ruff lint checks                                 |
| `make format`      | Format code with Ruff                                |
| `make typecheck`   | Run mypy                                             |
| `make up`          | Start app + Postgres + Redis via Docker Compose      |
| `make down`        | Stop the Docker Compose stack                        |
| `make rotate-key`  | Re-encrypt PHI columns onto a new key version (see "Key rotation") |
| `make worker`      | Run a Celery worker (`generate_note_task`) — starts Postgres + Redis first |
| `make beat`        | Run Celery beat (schedules `purge_expired_data_task`) — starts Postgres + Redis first |

## Configuration

All configuration is environment-driven (`app/config.py`), loaded via
`pydantic-settings`. See `.env.example` for every setting. Notable ones:

- `ENV`: `dev` | `staging` | `prod`. If `ENV=prod`, `DEBUG` must be `False`,
  and neither `SECRET_KEY` nor `JWT_SECRET` may look like a placeholder.
- `PHI_MODE`: `synthetic` | `real`. Later phases must enforce that
  production never runs in `synthetic` mode, and that `real` mode is only
  enabled once a BAA'd PHI vendor stack is verified.
- `JWT_SECRET` / `JWT_ALGORITHM`: JWT signing. Deliberately distinct from
  `SECRET_KEY` — see "Auth" below.
- `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` /
  `IDLE_TIMEOUT_MINUTES`: session timeout — see "Auth" below.
- `CORS_WEB_ORIGINS` / `CORS_EXTENSION_ORIGINS`: comma-separated explicit
  origin lists (never `"*"`); extension origins use the
  `chrome-extension://<id>` scheme.
- `TRANSCRIBER_VENDOR` (`mock` | `openai`) / `OPENAI_API_KEY` /
  `OPENAI_TRANSCRIBE_MODEL` / `TRANSCRIBE_TIMEOUT_SECONDS` /
  `TRANSCRIBE_MAX_RETRIES`: transcription vendor selection — see
  "Transcription" below.
- `MAX_SESSION_SECONDS` / `MAX_AUDIO_BYTES` / `WS_BUFFER_MAX_CHUNKS` /
  `TRANSCRIPT_RETENTION_DEFAULT` (`none` | `consented` | `always`): audio
  ingestion limits and retention policy — see "Audio ingestion" below.
- `NOTE_GENERATOR_VENDOR` (`mock` | `openai`) / `OPENAI_NOTE_MODEL` /
  `NOTE_MAX_RETRIES` / `NOTE_TIMEOUT_SECONDS` / `NOTE_SCRUB_ENABLED` /
  `NOTE_PROMPT_VERSION` / `NOTE_TRACING_ENABLED` / `LANGFUSE_HOST`: SOAP
  note generation — see "SOAP note generation" below.
- `KEY_PROVIDER` (`local` | `aws_kms`) / `LOCAL_ENCRYPTION_KEY` /
  `KMS_KEY_ID` / `ENCRYPTION_KEY_VERSION`, `ENFORCE_TLS` /
  `TRUST_PROXY_HEADERS`, `ALLOW_BLANKET_RETENTION`: the Phase 7 HIPAA
  controls — see "HIPAA controls" below.
- `RATELIMIT_ENABLED` + per-bucket `RATELIMIT_*_LIMIT`/`RATELIMIT_*_WINDOW_SECONDS`,
  `METRICS_ENABLED` / `METRICS_AUTH_TOKEN`, `LANGFUSE_PUBLIC_KEY` /
  `LANGFUSE_SECRET_KEY` / `TRACE_INCLUDE_CONTENT`, `CELERY_BROKER_URL` /
  `CELERY_RESULT_BACKEND`, `PURGE_UNRETAINED_AFTER` /
  `PURGE_ORPHAN_SESSION_AFTER` / `PURGE_INTERVAL` / `PURGE_DRY_RUN`: the
  Phase 8 hardening/operations controls — see "Hardening and operations"
  below.

## Auth

Stateless JWT auth (`app/auth/`, `app/api/auth.py`, mounted at
`/v1/auth`) designed to work identically for the Chrome extension, the
web app, and the mobile app.

### Why stateless

All three clients send `Authorization: Bearer <token>`; there is no
server-side session store on the access path — the access token itself
carries identity (`sub`), tenant (`practice_id`), and `role`. Access
tokens are returned in the **JSON response body**, never as an httpOnly
cookie: the extension's background service worker calls the API from a
`chrome-extension://` origin (not the web app's origin) and neither it
nor the mobile app can rely on browser cookie storage the way a
same-origin web app could. Bearer-in-body is the one mechanism that works
uniformly across all three.

### Endpoints

| Method | Path                  | Purpose |
|--------|------------------------|---------|
| POST   | `/v1/auth/register`   | Bootstrap: creates a practice + its first (owner) user, returns tokens |
| POST   | `/v1/auth/login`      | email+password → tokens, or an `mfa_pending` token if MFA is enabled |
| POST   | `/v1/auth/mfa/setup`  | Generates a TOTP secret (not yet enabled) |
| POST   | `/v1/auth/mfa/verify` | Confirms setup (access token) *or* completes an MFA login (`mfa_pending` token) — see below |
| POST   | `/v1/auth/refresh`    | Rotates refresh+access tokens; enforces idle timeout |
| POST   | `/v1/auth/logout`     | Revokes the presented refresh token |
| GET    | `/v1/auth/me`         | Current user's profile |

`GET /v1/users/{user_id}` (owner-only) and the auth endpoints above are
the only routes in this phase — enough to prove auth, RBAC, and RLS
wiring work end-to-end; the transcription/note pipeline's real business
routes come later.

### Session timeout = access TTL + refresh idle timeout

There are two independent clocks:

- **`ACCESS_TOKEN_TTL_MINUTES`** (default 15): how long an access token
  is valid at all, regardless of activity. A stolen access token is only
  useful for this long.
- **`IDLE_TIMEOUT_MINUTES`** (default 30): `POST /refresh` tracks
  `last_activity_at` on the server-side `refresh_tokens` row and refuses
  to rotate — 401 — once that's older than this, even if the refresh
  token itself hasn't hit its outer expiry yet. This is what actually
  implements "session timeout" for a long-lived client: no activity for
  30 minutes means the next refresh attempt fails and the client must log
  in again.
- **`REFRESH_TOKEN_TTL_DAYS`** (default 7): the hard outer ceiling on a
  refresh token regardless of activity.

### Refresh rotation and reuse detection

Every `/refresh` call revokes the presented `jti` and issues a new one
(`app/auth/refresh_store.py`). If a `jti` that's already revoked is
presented again — meaning either a stolen token was used after the
legitimate client already rotated past it — every refresh token for that
user is revoked and an `AuditAction.token_reuse_detected` entry is
written, then a 401 returned. The user must log in again on every device.

### MFA (scaffolded, off by default)

`User.mfa_enabled` gates everything; nothing here changes behavior for a
user who hasn't opted in. Flow:

1. `POST /mfa/setup` (with a normal access token) generates a TOTP secret
   and returns it plus a `provisioning_uri` for an authenticator app. This
   does **not** yet set `mfa_enabled` — a typo'd confirmation would
   otherwise lock the user out.
2. `POST /mfa/verify` with that same access token + a valid code confirms
   setup and sets `mfa_enabled = true`.
3. From then on, `POST /login` with correct credentials returns an
   `mfa_pending` token (5-minute TTL) instead of real tokens.
4. `POST /mfa/verify` with the `mfa_pending` token (as the bearer) + a
   valid code exchanges it for real access + refresh tokens — the same
   endpoint as step 2, dispatched on which kind of token it's given.

### Rate limiting

`/login` and `/refresh` are rate-limited per client IP via the central
Redis-backed limiter (`app/ops/ratelimit.py`, Phase 8's consolidation of
the earlier Phase 3 stub) — lenient today, but the hook is real:
tightening any bucket is a config change, not new plumbing. See
"Hardening and operations" below for the full rate-limiting picture
(other routes, WS connect limiting, `Retry-After`).

### Audit trail

`register`, `login_success`, `login_failure`, `logout`, `mfa_enrolled`,
`mfa_verified`, and `token_reuse_detected` are all written to `audit_logs`
(PHI-free metadata only — see "Immutable audit log" below). `login_failure`
is only logged when the email matched a real user (so there's a tenant to
attribute it to); an unknown-email attempt is covered by the rate limiter
instead, not an audit row, since there's no tenant to write one against.

## Transcription

`app/transcription/` is a vendor-agnostic transcription interface. The
entire point is isolation: **nothing outside `app/transcription/` may
import or know about a vendor SDK** — not the audio ingest that'll call
this in a later phase, not note generation, nothing. This is enforced
three ways at once: `pyproject.toml`'s `flake8-tidy-imports` banned-api
rule fails `make lint` if any file other than
`app/transcription/openai.py` imports `openai`; the factory only imports
`openai` lazily inside its "openai" branch; and
`tests/test_transcription_isolation.py` statically AST-scans every file
under `app/` for an `openai` import and also dynamically asserts
selecting the mock vendor never touches `sys.modules["openai"]`.

### The interface (`app/transcription/base.py`)

- `Transcriber.transcribe(chunks, *, language=None) -> TranscriptionResult`
  — batch/whole-utterance transcription, the primary path for a
  file-based vendor like OpenAI.
- `Transcriber.stream(chunks) -> AsyncIterator[TranscriptionEvent]` —
  partial/final events as audio arrives.
- `AudioChunk`, `TranscriptSegment`, `TranscriptionResult`,
  `TranscriptionEvent` are plain dataclasses, not vendor types.
  `TranscriptSegment.to_jsonb()` produces exactly the shape Phase 2's
  `Transcript.segments` JSONB column expects
  (`{speaker, start, end, text}`) — persistence never needs to know which
  vendor produced a result.
- `speaker` is `None` for every v1 vendor (OpenAI does not diarize) but
  exists on `TranscriptSegment` from day one so a diarizing vendor can
  start populating it later with zero shape changes downstream.
- Typed exceptions — `TranscriptionError` (base),
  `TranscriptionAuthError`, `TranscriptionRateLimitError`,
  `TranscriptionTimeoutError` — are the only things a caller should ever
  catch. Every implementation is required to translate vendor errors into
  these; `app/transcription/openai.py`'s `_translate_error` is the
  reference example.

### Streaming emulation

OpenAI's file-based transcription API has no true real-time streaming
for this use case. `OpenAITranscriber.stream()` buffers incoming chunks
into fixed-size windows (`_DEFAULT_STREAM_WINDOW_CHUNK_COUNT` chunks per
window), calls `transcribe()` once per window, and yields a single
`"final"` `TranscriptionEvent` per window — never `"partial"`, since
there's no visibility into a window's transcript until the vendor call
for it completes. `transcribe()`'s own windowing is a *different*
concern: it splits by cumulative byte size (`_MAX_WINDOW_BYTES`, staying
under OpenAI's upload limit) rather than chunk count, and stitches the
per-window results back into one `TranscriptionResult` in order. A
genuinely streaming vendor (e.g. Deepgram) would override `stream()`
entirely with a real partial-event cadence instead of emulating one this
way.

### Vendor selection (`app/transcription/factory.py`)

`TRANSCRIBER_VENDOR` (`mock` | `openai`, default `mock`) picks the
implementation, with guardrails that fail application startup rather
than fail some request later:

- `PHI_MODE=synthetic` **always** forces `mock`, regardless of
  `TRANSCRIBER_VENDOR` — real vendors only ever handle real, BAA-covered
  audio, and synthetic (dev/test) data must never leave the process.
- `ENV=prod` with a vendor that resolves to `mock` fails fast — a real
  vendor is required in production.
- `TRANSCRIBER_VENDOR=openai` without `OPENAI_API_KEY` set fails fast.
- **Before `TRANSCRIBER_VENDOR=openai` ever runs against real (non-
  synthetic) audio**, a signed BAA *and* a Zero Data Retention agreement
  with OpenAI must be in place. The factory has no way to verify that
  operationally — it's a deployment precondition, not a check code can
  make — so this is a process requirement, not just a config flag.

`get_transcriber()` is a process-wide `@lru_cache` singleton (matching
the zero-arg convention already used by `app/db/session.py`'s
`get_engine()`/`get_sessionmaker()`); `build_transcriber(settings)` is
the pure, unmemoized selection logic underneath it, for tests that need
a custom `Settings`. `app/main.py`'s lifespan calls `get_transcriber()`
at startup — both so a misconfigured vendor fails immediately instead of
on a patient's first recorded visit, and so the selected provider name
(never content) gets logged once, at boot.

### Mock (`app/transcription/mock.py`)

`MockTranscriber` needs no network and no keys: it returns a
deterministic, synthetic primary-care-visit transcript fixture. It's the
default whenever `PHI_MODE=synthetic` (i.e. always in dev/test) and the
only transcriber this test suite ever actually calls. Constructor flags
(`latency_seconds`, `simulate_rate_limit`, `simulate_empty_audio`) let
tests exercise those paths deterministically without a real vendor
outage or real timing.

## Audio ingestion

`app/api/sessions.py` (mounted at `/v1/sessions`) and
`app/services/transcription_service.py` wire the Phase 4 `Transcriber`
interface to real sessions. This phase ends at a stored transcript — no
SOAP note generation (`SessionStatus.generating` exists on the model but
nothing here ever sets it).

### Session lifecycle

- `POST /v1/sessions` — creates a session (`status=created`) for the
  caller's practice; returns `{session_id}`.
- `GET /v1/sessions/{id}` — status + whether a transcript exists.
  Tenant-scoped like every other route: a guessed id from another
  practice comes back 404, not 403 (RLS hides it — see `app/api/users.py`
  for the same established pattern).
- `POST /v1/sessions/{id}/finalize` — non-streaming fallback: upload a
  whole recording (multipart), get `{transcript}` back. Shares
  `app.services.transcription_service.persist_transcript` with the WS
  path, so both are identical from "transcribed" onward.
- Status transitions this phase drives: `created` → `recording` →
  `transcribing` → `complete` (or → `error` on any failure).

### WebSocket (`/v1/sessions/{id}/stream`)

**Auth**: the access token is passed as a query parameter
(`?token=<access_token>`), not a header — a browser/extension WS
handshake can't reliably set `Authorization`. Validated identically to
`get_current_user` (decode → active user → tenant match) before anything
else happens; failure closes with code `4401`. An unknown or
another-practice's session closes `4404` (same RLS-hides-it reasoning as
the REST routes above — there's no way to distinguish "doesn't exist"
from "exists, wrong tenant" without leaking existence, so this
deliberately doesn't try to use `4403`).

**Protocol**:
- Client → server: binary frames are raw audio chunks. JSON text frames:
  `{"type": "start", "format": {"mime": ..., "sample_rate": ...}}` and
  `{"type": "stop"}`.
- Server → client (JSON): `{"type": "session.status", "status": ...}`,
  `{"type": "transcript.partial", "text": ...}`,
  `{"type": "transcript.final", "text": ..., "segments": [...]}`,
  `{"type": "error", "code": ..., "message": ...}` (`message` is always
  PHI-free).

**Buffering/backpressure**: incoming audio goes onto a bounded
`asyncio.Queue` (`WS_BUFFER_MAX_CHUNKS`) that feeds `transcriber.stream()`
through an async-generator bridge; a client producing audio faster than
it's consumed blocks on `queue.put` rather than growing the buffer
without limit. `MAX_AUDIO_BYTES` / `MAX_SESSION_SECONDS` are enforced on
every incoming frame — exceeding either sends an `error` event and closes
`4413`.

**Zero-retention**: raw audio is never written to disk, S3, or any
database column, anywhere in this phase. Every point audio is actually
held — the WS queue, the finalize route's in-memory upload — is marked
with a `NEVER-PERSIST` comment. `tests/test_sessions_ws.py` backs this
with both a dynamic check (spies on `open()` for any write-mode call
during a full streaming session) and a static one (AST-scans
`app/api/sessions.py` and `app/services/transcription_service.py` for
disk/object-storage API names), the same two-layer technique Phase 4
uses for its vendor-isolation guardrail.

### Retention gating

`Transcript.is_retained` is never implied by the row merely existing (see
`app/models/transcript.py`). `TRANSCRIPT_RETENTION_DEFAULT` governs the
default when no session-scoped `Consent(consent_type=retention,
granted=True)` exists: `"none"` never retains, `"consented"` (default)
retains only with an explicit consent on file, `"always"` retains
unconditionally. When retention is denied, a `Transcript` row is still
created (so "does a transcript exist" stays meaningful and re-finalizing
stays idempotent) but with empty `content`/`segments` — the caller who
just submitted the audio still gets the real transcript back in the HTTP/
WS response; only what's persisted is gated.

### Audit trail additions

`session.created`, `stream.started` (a new `AuditAction` enum value —
see migration `6243b19b618c`), and `transcript.created` are written via
`app.services.transcription_service.record_ingestion_event`, PHI-free
(provider name, counts, durations — never transcript text). A failed
transcription also writes an audit entry (`action=update`,
`metadata={"status": "error", "reason": <error code>}`) and sets the
session to `error`.

## SOAP note generation

`app/notes/` is a vendor-agnostic SOAP note generation interface — the
same isolation rule as `app/transcription/` applies: **nothing outside
`app/notes/` may import or know about a vendor SDK**. Enforced the same
three ways: `pyproject.toml`'s banned-api rule, the factory's lazy
`openai` import, and `tests/test_transcription_isolation.py` (the shared
full-tree AST scan, now allowlisting both `app/transcription/openai.py`
and `app/notes/openai.py`) plus `tests/test_notes_isolation.py` (the
notes-specific dynamic check). `app/services/note_service.py` wires it
to a Phase 5 transcript; since Phase 8, `app/api/notes.py`'s
`POST /v1/sessions/{id}/note` no longer calls it directly — it enqueues
a Celery task instead. See "Hardening and operations" below for the
async enqueue/poll flow. Specialty is locked to Primary Care, format to
SOAP.

### The interface (`app/notes/base.py`)

- `NoteGenerator.generate_soap(transcript_text, context) -> SoapNote` —
  the only method. `NoteContext` carries `specialty` (`"primary_care"`
  only, for now), `language`, and an unused `clinician_preferences`
  placeholder for a later phase.
- `SoapNote` (`subjective`, `objective`, `assessment`, `plan`, `full_text`,
  `provider`, `model`) is strict by construction: every section is
  required and non-empty, `extra="forbid"` rejects stray fields, and
  `SoapNote.compose(...)` is the one place `full_text` gets built from
  the four sections, so every implementation composes it identically.
  This validates the *shape a generator returns* — not a vendor's raw
  response; `app/notes/openai.py`'s `_RawSoapOutput` validates that
  separately, before it's ever allowed to become a `SoapNote` (see
  "Retry-then-degrade" below).
- Typed exceptions — `NoteGenerationError` (base), `NoteAuthError`,
  `NoteRateLimitError`, `NoteTimeoutError`, `NoteValidationError` (raised
  once retries are exhausted) — are the only things a caller should ever
  catch, mirroring `app/transcription/base.py`'s exception set exactly.
- `NoteGenerator.last_retry_count` is a plain instance attribute (not
  part of `SoapNote`'s schema) that `generate_soap()` sets on every call
  — generation metadata for the audit trail, not part of the note.

### Scrubbing (`app/notes/scrub.py`)

A conservative, non-LLM, rule-based pre-step strips clearly non-clinical
chatter (greetings, small talk, sign-offs) before the transcript reaches
the generator. The bias is deliberately toward *keeping* a sentence: it's
only dropped when it matches a chatter phrase pattern **and** contains no
clinical-content marker — a sentence that reads as small talk but carries
a clinical detail (e.g. "By the way, I've also noticed some swelling in
my ankles.") survives. Toggle with `NOTE_SCRUB_ENABLED` (default `true`).
The original `Transcript` row is never touched — scrubbing only ever
produces a new in-memory string.

### Retry-then-degrade

`OpenAINoteGenerator` requests JSON output, parses it, and validates it
against `_RawSoapOutput` (exactly four required, non-empty keys —
`extra="forbid"` makes "the model added/omitted a key" a validation
failure). On failure, the validation error is fed back into the next
attempt's prompt ("your previous response failed schema validation:
...") for up to `NOTE_MAX_RETRIES` retries; transient/rate-limit errors
get the same exponential-backoff-with-jitter retry as
`app/transcription/openai.py`. If every attempt fails, `generate_soap`
raises `NoteValidationError` (or whatever typed error the last attempt
produced) — it never returns a partial or invalid note.

`app/services/note_service.py`'s **DEGRADATION POLICY** catches that:
the visit is never lost. A `Note` stub is persisted (`status=draft`,
`degraded=True`, all four SOAP sections `None`, `full_text` set to the
**raw transcript**) so a poller reading `GET /v1/sessions/{id}/note`
always has something to read in `note.full_text` — the transcript itself
when degraded, the composed SOAP text otherwise — **provided retention
consent allowed it to be persisted at all** (see the Phase 8 note below).
The session moves to `SessionStatus.complete_degraded` (a new enum
value, migration `6e70ea786d86`) rather than `complete`, so a session
list can tell "note ready" apart from "needs manual note writing"
without joining `notes`. Audit entries, same style as Phase 5's:
`note.created` (`action=create`, `resource_type="note"`) on success,
`note.degraded` (a new `AuditAction` value) on degradation — both
PHI-free (provider, model, `prompt_version`, `retry_count`, `degraded` —
never note/transcript text).

**Phase 8 change**: before Phase 8, `POST /v1/sessions/{id}/note` ran
generation synchronously and its HTTP response always carried the real
content regardless of `is_retained` (retention only governed what got
*written*, never what the caller who just triggered generation was
*told*). Now that generation happens on a worker and the result is only
ever read back via `GET .../note`, that guarantee can't hold — a poller
can only ever see what actually made it into the `notes` table. When
`is_retained` is `false`, `GET .../note` legitimately returns empty
sections (including `full_text`, even on a degraded outcome) once
generation finishes. This is an inherent consequence of async processing
plus zero/consented retention, not a bug — see `app/api/notes.py`'s
module docstring for the full reasoning.

### Prompt versioning (`app/notes/prompts/soap_primary_care_v1.py`)

The Primary-Care SOAP system prompt lives in its own versioned module —
`PROMPT_VERSION = "soap_primary_care_v1"` — rather than an inline string,
so a prompt change is a new file (`soap_primary_care_v2.py`, ...), never
an in-place edit. `Note.prompt_version` persists which exact prompt
produced a given note, on both success and degraded outcomes (the prompt
was still attempted even when generation ultimately failed).

### Vendor selection (`app/notes/factory.py`)

`NOTE_GENERATOR_VENDOR` (`mock` | `openai`, default `mock`) with the
identical guardrail shape as `app/transcription/factory.py`:
`PHI_MODE=synthetic` always forces `mock`; `ENV=prod` with a vendor that
resolves to `mock` fails fast; `NOTE_GENERATOR_VENDOR=openai` without
`OPENAI_API_KEY` fails fast; and before real (non-synthetic) transcript
text ever reaches OpenAI, a signed BAA *and* Zero Data Retention agreement
must be in place (a deployment precondition the factory can't verify).
`get_note_generator()` is the process-wide `@lru_cache` singleton
`app/main.py`'s lifespan calls at startup, same fail-fast-at-boot
reasoning as `get_transcriber()`.

### LLM tracing

Off by default (`NOTE_TRACING_ENABLED=false`). If enabled, `LANGFUSE_HOST`
must point at a **self-hosted** Langfuse instance — `app/config.py`
refuses to start if it looks like Langfuse Cloud, because tracing would
otherwise send transcript/note PHI to a third party. Since Phase 8, this
is a real integration (`app/ops/tracing.py`, `Tracer`/`get_tracer()`),
wired at the `app/services/note_service.py` orchestration boundary —
once per `generate_note_for_session()` call, not once per vendor retry
attempt. See "Hardening and operations" below for the full picture
(what a trace carries, `TRACE_INCLUDE_CONTENT`, and the three
independent layers that refuse Langfuse Cloud).
`app/notes/openai.py`'s own `_trace_generation` stays a separate,
PHI-safe no-op hook for a possible future finer-grained (per-retry-
attempt) trace — not used by the Phase 8 integration.

## HIPAA controls

`app/security/` (Phase 7) hardens what Phases 1-6 built — no new product
surface. Five things, each addressed below: field-level encryption at
rest, TLS in transit, PHI *read* access auditing, a centralized consent
gate before retention, and a fail-fast startup self-check.

### Field-level encryption (`app/security/`)

Envelope encryption, AES-256-GCM (authenticated — tampering is detected,
not silently accepted): every encrypted value gets its own random
256-bit data-encryption key (DEK), used once to encrypt that value; the
DEK is then wrapped (encrypted) by a `KeyProvider`'s key-encryption key
(KEK) and stored alongside the ciphertext in one self-describing blob —
`{key_version, wrapped_dek, nonce, ciphertext}`, base64-encoded behind a
`phi-enc-v1:` marker (`app/security/encryption.py`). The KEK never
touches plaintext PHI directly, only ever wraps/unwraps per-value DEKs.

`KeyProvider` (`app/security/keys.py`) has two implementations, selected
by `KEY_PROVIDER`: `LocalKeyProvider` (`local`, the default — a static
key from `LOCAL_ENCRYPTION_KEY`, no AWS needed, the only one the test
suite exercises) and `AwsKmsProvider` (`aws_kms`, production — lazily
imports `boto3`, never exercised in CI, refused at both this factory and
`app/security/startup_checks.py` whenever `ENV=prod` still has
`KEY_PROVIDER=local`).

**PHIText is the seam.** Phase 2 reserved `PHIText` (`app/db/types.py`)
as a plain `Text` pass-through specifically so encryption could swap in
later without touching call sites or migrating each table separately —
it's now `EncryptedText`, so every existing `PHIText` column encrypts
automatically. `transcripts.segments` (structured JSONB, not text) uses
the parallel `EncryptedJSON` type, which serializes to JSON text first —
its SQL column type changed from `JSONB` to `Text` in migration
`e545adfa522b`, since Postgres can no longer validate encrypted content
as JSON.

**Encrypted columns**: `transcripts.content`, `transcripts.segments`,
`notes.subjective`/`objective`/`assessment`/`plan`/`full_text`,
`sessions.patient_ref`, `users.mfa_secret` — every column that can hold
PHI or a PHI-adjacent secret.

**Not encrypted: `users.email`.** It's workforce login data (not patient
PHI) used to look up which practice a login belongs to
(`app/api/auth.py`'s login flow) and enforced unique at the database
level — both require it to be plaintext and indexable. Encrypting it
would make login require decrypting every user row to find a match (or
an HMAC blind index — a deterministic keyed hash stored alongside the
encrypted value, matched by hash instead of plaintext) — not implemented
here; documented as the option if this changes. **General rule: an
encrypted column cannot be indexed, filtered, or searched by Postgres**
— nothing in this schema encrypts a column anything queries by.

**Legacy-data tolerance**: `decrypt_value` treats any stored value
*without* the `phi-enc-v1:` prefix as legacy, pre-encryption plaintext
and returns it unchanged, rather than erroring — this is what lets
migration `e545adfa522b`'s data-migration step (which encrypts every
existing plaintext PHI value in place, table by table) run safely
against a mix of already-migrated and not-yet-migrated rows, and be
re-run/resumed. Dev/test databases are synthetic and typically empty at
migration time, so reseeding is equally valid there (see
`tests/conftest.py`); this path is for anywhere with real existing rows.

**Key rotation** (`scripts/rotate_encryption_key.py`, `make rotate-key`):
moves every value from an old key version to a new one — decrypt with
old, re-encrypt with new — skipping rows already on the new version
(safe to resume) and legacy unencrypted rows (not its job). Needs
`ROTATE_OLD_KEY_VERSION` / `ROTATE_OLD_LOCAL_ENCRYPTION_KEY` /
`ROTATE_NEW_KEY_VERSION` / `ROTATE_NEW_LOCAL_ENCRYPTION_KEY` in the
environment (deliberately not part of `Settings` — the running app
should never hold two keys at once; a rotation, briefly, does). Add
`ARGS=--dry-run` to preview. Not "live" (no batching for a huge table,
no online cutover coordination) — the straightforward, correct path,
documented rather than built further since nothing here has that scale
yet. `LocalKeyProvider` accepts multiple key versions at once
specifically so a provider mid-rotation (or this script itself) can
decrypt values still on the old version and values already on the new
one simultaneously.

**At-rest, infrastructure level**: field-level encryption above is the
*application* layer — it holds regardless of what the database or its
backups are doing. Infra-level at-rest encryption (RDS encryption, S3
SSE-KMS, encrypted EBS/backups) is a separate, equally required control
that belongs in the infra/Terraform layer, not this codebase; the app
must never assume unencrypted storage is an acceptable fallback.

### TLS enforcement and security headers (`app/security/transport.py`)

`TLSEnforcementMiddleware` is a pure ASGI middleware (deliberately not
Starlette's `BaseHTTPMiddleware`, which never sees `websocket` scope at
all) so it covers both HTTP requests and WebSocket handshakes. Governed
by `ENFORCE_TLS` (default `true`; `.env.example` turns it off for local
HTTP dev): an insecure HTTP request gets a flat `400`, never a redirect
(a redirect still has to be sent over the insecure connection first,
exactly the exposure this closes); an insecure (`ws://`, not `wss://`)
WebSocket handshake is closed with code `4400` before ever being
accepted. Behind a TLS-terminating load balancer, the app only sees
plain HTTP/WS itself — `TRUST_PROXY_HEADERS` (default `false`) opts in
to trusting `X-Forwarded-Proto` for the "was this actually HTTPS"
determination; leave it off unless a deployment genuinely sits behind a
proxy that sets that header itself and strips any client-supplied copy.

`SecurityHeadersMiddleware` adds baseline headers to every HTTP response
(`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`) and, whenever `ENFORCE_TLS` is on, HSTS
(`max-age=63072000; includeSubDomains`) — omitted when TLS isn't being
enforced, since an HSTS header over a connection that isn't guaranteed
HTTPS is actively misleading.

### PHI read-access auditing (`app/services/audit_service.py`)

Phase 2 audited writes and made `audit_logs` immutable (still enforced
both at the ORM level and via `REVOKE UPDATE, DELETE` at the database
level — see "Immutable audit log" below). HIPAA requires logging PHI
*access* — reads too. `record_phi_access` is the one choke point for
both; `app/services/transcription_service.py`'s `record_ingestion_event`
(the name the Phase 5/6 call sites already used) is now a thin wrapper
around it, so existing write-audit call sites needed no changes.

Call it once per **logical operation**, never per decrypted field —
`app/security/encrypted_type.py`'s TypeDecorators are deliberately *not*
the hook point: that fires once per column per row, which would be noisy
and isn't what access logging is for. `app/services/note_service.py`'s
`generate_note_for_session` is the concrete example: it reads the stored
transcript before generating a note from it, and that read is audited
(`action=read, resource_type="transcript"`) exactly like a write, with
PHI-free metadata and `ip_address`/`user_agent` captured from the
request where one is available (`request_meta_from_request`). A
lightweight static guard (`tests/test_phi_access_audit_guard.py`)
AST-scans the service layer for functions that fetch a `Transcript`/
`Note` row without also referencing an audit call anywhere in their
body — a heuristic, backed by the real behavioral test in
`tests/test_note_service.py`.

### Consent gate (`app/security/consent.py`)

`ConsentService.assert_retention_allowed` is the single authority
deciding whether transcript/note PHI may be persisted — it replaces the
per-caller discretion Phases 5 and 6 used to each resolve independently
(Phase 6's note persistence didn't gate retention *at all* before this).
Both `app/services/transcription_service.py`'s `persist_transcript` and
`app/services/note_service.py`'s `_persist_success`/`_persist_degraded`
call it before writing. Policy is `TRANSCRIPT_RETENTION_DEFAULT`:
`"none"` never retains; `"consented"` (default) retains only with an
explicit granted `Consent` (type `recording` or `retention`) scoped to
the session or the whole practice; `"always"` retains unconditionally
**but requires the explicit `ALLOW_BLANKET_RETENTION` opt-in** — for a
practice under a genuine signed blanket retention agreement, never as a
convenient default. **Risk**: `"always"` bypasses the per-session
consent check entirely; `app/security/startup_checks.py` additionally
refuses to boot with `PHI_MODE=real`, this policy, and no opt-in.

When retention isn't allowed, `Transcript`/`Note` rows are still created
(so "does one exist" stays meaningful and re-finalizing stays
idempotent) but with empty/`None` content and `is_retained=False` — only
what's persisted is gated. For `Transcript` (still a synchronous request
path), the caller who just submitted the audio still gets the real
content back in that same HTTP response. For `Note`, since Phase 8 moved
generation onto a worker (see "Hardening and operations" below), there
is no synchronous response to carry real content in any more — a poller
reading `GET /v1/sessions/{id}/note` genuinely only ever sees what made
it into the table, gated content included. The decision itself is
audited (`AuditAction.retention_skipped`, PHI-free) alongside the
`create` audit entry, whichever way it goes.

`ConsentService.assert_training_allowed` is the training-data
belt-and-suspenders: no model-training/improvement path exists anywhere
in this codebase yet, but any future one MUST call this (requiring an
explicit granted `Consent(training)`) *in addition to*, never instead
of, the vendor BAA/Zero-Data-Retention guarantee already required for
`TRANSCRIBER_VENDOR=openai`/`NOTE_GENERATOR_VENDOR=openai`.

### Startup self-check (`app/security/startup_checks.py`)

`run_startup_safety_checks(settings)` runs in `app/main.py`'s lifespan
before vendor selection or serving any traffic — raising aborts FastAPI's
startup, crashing the process with a non-zero exit. **Refuses to start
when:**

- `ENV=prod` and `DEBUG=true` (Phase 1's rule, re-asserted here as
  defense in depth alongside `Settings`' own validator).
- `ENV=prod` and `PHI_MODE=synthetic`.
- `PHI_MODE=real` and any of: `KEY_PROVIDER=local`; `TRANSCRIBER_VENDOR`
  or `NOTE_GENERATOR_VENDOR` is `mock`; either is `openai` with no
  `OPENAI_API_KEY`; `ENFORCE_TLS=false`; (`ENV=prod` and) `ENFORCE_TLS`
  is on but `TRUST_PROXY_HEADERS` is off (every request would be
  incorrectly rejected behind a TLS-terminating load balancer);
  `TRANSCRIPT_RETENTION_DEFAULT=always` without `ALLOW_BLANKET_RETENTION`.
- (Phase 8) `NOTE_TRACING_ENABLED=true` with no self-hosted
  `LANGFUSE_HOST` — re-asserts, independently, the same rule `Settings`'
  own `_validate_tracing_is_self_hosted` validator already enforces at
  construction time.
- (Phase 8) `TRACE_INCLUDE_CONTENT=true` with `PHI_MODE=real` or
  `ENV=prod` — same defense-in-depth re-assertion of `Settings`'
  `_validate_trace_content_restricted` validator.

On success, it logs a single PHI-free "HIPAA controls engaged" banner —
which provider/vendor/policy is selected for every control above (Phase
8's rate limiting/metrics/tracing/purge settings included), never key
material or API keys — so it's obvious at a glance, in production
startup logs, what's actually active rather than assumed.

## Hardening and operations

Phase 8 hardens and operationalizes what Phases 1-7 built — no new
product surface. Five things: rate limiting, metrics, LLM tracing, async
note generation, and a scheduled retention-purge job.

### Rate limiting (`app/ops/ratelimit.py`)

A single central, Redis-backed, fixed-window limiter — replaces/
consolidates the Phase 3 `/login`/`/refresh` stub. Keying: authenticated
routes use `UserRateLimiter`, keyed `user:<id>:practice:<id>` (the limit
travels with the account, not the network address — important once a
whole clinic shares one IP/NAT); pre-auth routes (`/login`, `/refresh`)
use `IpRateLimiter`, keyed `ip:<addr>` (there's no verified identity
yet). A rejected request gets `429` with a `Retry-After` header. Limits
live in Redis, which every app-server process/instance shares — they
hold cluster-wide, not just within one process, so scaling `app`
horizontally doesn't multiply any bucket's effective ceiling.

Wired onto: `/login`, `/refresh` (stricter), session creation and note
generation (moderate), plain reads (`GET .../{id}`, `GET .../note` —
looser), and WebSocket `/stream` **connection establishment** — a client
opening many concurrent/rapid streaming connections is throttled once,
per connection attempt, never per audio frame within an already-open
one. `RATELIMIT_ENABLED=false` bypasses the limiter entirely (useful for
load testing); each bucket's limit/window is independently configurable
via `RATELIMIT_<BUCKET>_LIMIT`/`RATELIMIT_<BUCKET>_WINDOW_SECONDS` — see
`.env.example`.

### Metrics (`app/api/metrics.py`, `app/ops/metrics.py`, `app/ops/http_metrics.py`)

`GET /metrics` exposes Prometheus-format output: HTTP request
count/latency by route template + method + status, in-flight request
gauge, rate-limit-hit counter (by bucket), WS active-connection gauge +
session-duration histogram, transcription/note-generation latency by
provider + outcome, Celery task count/latency by task + outcome, and a
purge-job rows-deleted counter by resource type.

**Every label is drawn from a small, fixed, known-in-advance vocabulary
— route template (never a raw path with an id substituted in), HTTP
method, status code, vendor/provider name, task name, outcome.** Never a
patient/session/transcript/user identifier or free text — see
`app/ops/metrics.py`'s module docstring and `tests/test_metrics.py`,
which asserts the exposition output contains no UUID anywhere.

**Known scope boundary**: the Celery task and purge-job series live in
the collector registry of whichever process incremented them. In tests
(`task_always_eager`), that's the same process serving `/metrics`, so
they show up correctly — `tests/test_metrics.py` asserts on exactly
that. In `docker-compose.yml`'s real multi-container topology, `worker`/
`beat` run as separate processes with no HTTP server of their own, so
their metrics aren't currently scrapeable from `app`'s `/metrics`. A
production deployment wanting worker-side series needs one of:
`prometheus_client`'s official multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`
+ file-based aggregation — the standard answer for a prefork pool with
several child processes, each of which would otherwise fight over one
port), or a lightweight `start_http_server(...)` call gated on Celery's
`worker_process_init` signal if the worker runs with `--pool=solo`/
`--pool=threads` (one process, no port conflict). Neither is wired in
here — a deliberate line drawn given this phase's scope, not an
oversight to silently work around.

**Not a public endpoint.** `METRICS_AUTH_TOKEN`, when set, requires a
matching `Authorization: Bearer <token>` header; when unset, this relies
entirely on network policy (an internal-only ingress/firewall rule,
scraped by an in-network Prometheus) to keep it off the public internet
— see `docker-compose.yml`'s top-of-file comment for the scrape target.

### LLM tracing

See "SOAP note generation" → "LLM tracing" above for the self-hosted-
only rule (three independent layers refuse Langfuse Cloud: a `Settings`
validator, the startup self-check, and `app/ops/tracing.py`'s own
client-construction guard). What a trace carries: `prompt_version`,
provider, model, `retry_count`, `degraded`,
`outcome`, latency, and transcript/note **character counts** — never the
text itself, unless `TRACE_INCLUDE_CONTENT=true` (default `false`,
refused at startup under `PHI_MODE=real` or `ENV=prod` — synthetic/dev
prompt debugging only). A trace-backend failure never breaks note
generation — emission errors are caught and logged, not raised.

### Async note generation (`app/worker/`)

`generate_note_task` (Celery, Redis broker/result backend on separate
logical Redis DBs from `REDIS_URL`'s own) wraps
`app/services/note_service.py`'s `generate_note_for_session` **with no
rewrite of that function** — it was kept a plain async callable taking
only UUIDs back in Phase 6/7 specifically so this move needed none. The
task re-establishes RLS tenant context itself (`tenant_session`, same
helper the WS handler already uses) since a worker process has no
FastAPI request to inherit it from — a task enqueued for Practice A's
session cannot touch Practice B's data, RLS-enforced exactly as any
other code path. Task args are `str(uuid)` only, never PHI (Celery's
result backend stores/can log task args).

**Endpoint contract**: `POST /v1/sessions/{id}/note` validates the
session is ready, marks it `generating`, enqueues the task, and returns
`202 {task_id, status: "generating"}` immediately — it no longer runs
generation on the request thread. `GET /v1/sessions/{id}/note` polls:
`{status, note}`, where `status` mirrors the session's own lifecycle and
`note` is populated once the session reaches `complete` or
`complete_degraded`. Clients (extension/web/mobile) poll this route; a
push mechanism (WS notification or webhook) is a natural future addition
but out of scope here. See "SOAP note generation" above for how this
changes what a poller can see when `is_retained=false`.

**Idempotency**: before doing any work, the task checks whether a `Note`
already exists for the session and skips (rather than regenerating) if
so — safe against a duplicate enqueue or a Celery-level retry re-running
after the underlying work already completed. **Retries**: bounded,
jittered, and narrowly scoped to `TranscriptNotReadyError` (a
legitimate, rare race against the finalize/WS-persist path still
committing) — never `SessionNotFoundError` (the route already confirmed
the session existed moments before enqueueing; a retry can't fix it
having vanished since). Vendor-level retry/degradation is unchanged from
Phase 6: it happens *inside* `generate_soap()`/`generate_note_for_session`
before this task ever sees an exception — the degradation path (persist
a stub, audit `note.degraded`) runs identically whether triggered from
this task or (in tests) called directly.

Run a worker with `make worker` (or `uv run celery -A
app.worker.celery_app worker --loglevel=info`); `docker-compose.yml`'s
`worker` service does the same, sharing the `app` image/environment so
it always runs the exact same code. Scale it independently of the web
tier: `docker compose up --scale worker=3`.

### Retention purge (`app/worker/tasks.py`'s `purge_expired_data_task`)

Consent/retention (`ConsentService`) only ever governs what gets
persisted **at write time** — without a purge job, an unretained row
that briefly existed before a client read it, or an abandoned/error
session, would sit in the database forever. This Celery-beat-scheduled
task (interval: `PURGE_INTERVAL`, default `1h`) is what makes
zero/consented retention true *over time*.

**Deletes**: `Transcript`/`Note` rows with `is_retained=false` older
than `PURGE_UNRETAINED_AFTER` (default `24h`); orphaned/stale sessions
(`created`/`recording`/`error` status, never reached a terminal state)
older than `PURGE_ORPHAN_SESSION_AFTER` (default `24h`) — defensively
skipped if the session still carries a *retained* transcript or note,
even though both already CASCADE-delete with their session at the DB
level, since purging retained clinical content via the "orphan" path
would be a compliance bug, not cleanup; and expired `refresh_tokens`
(`expires_at < now()` **only** — a revoked-but-not-yet-expired token is
deliberately kept, since purging it early would defeat
`app/auth/refresh_store.py`'s reuse-detection check, which depends on
finding that row).

**Never touches**: `audit_logs`. This task has no code path that
imports or queries the `AuditLog` model at all beyond *writing* its own
summary rows — and even a hypothetical future bug here couldn't delete
one anyway, since the application DB role has `UPDATE`/`DELETE` revoked
on that table at the database level (see "Immutable audit log" below).

**Tenant scoping**: the admin/migration role is used only to enumerate
which practices exist (the same narrow cross-tenant-listing exception
documented on `app/db/session.py`'s `get_admin_engine`); every actual
delete runs through `tenant_session`, RLS-scoped to one practice at a
time, exactly like every other write path in this codebase — never a
blanket cross-tenant `DELETE`.

**Dry run by default** (`PURGE_DRY_RUN=true`): counts what *would* be
deleted and writes the same PHI-free summary audit row, without deleting
anything. Flip to `false` only after reviewing dry-run output. Either
way, one `AuditAction.purge_completed` row is written **per practice per
run** (`resource_type="purge"`, `actor_user_id=None` — system-initiated,
metadata is counts only: `transcripts_deleted`, `notes_deleted`,
`sessions_deleted`, `refresh_tokens_deleted`, `dry_run`) and one
Prometheus counter increment per resource type actually purged.

## Logging and PHI

All logs are structured JSON on stdout. A redaction filter
(`app/logging.py`) scrubs the value of any field whose key matches a known
PHI-sensitive name (e.g. `transcript`, `note`, `patient_name`, `ssn`, `dob`)
before it is ever serialized, regardless of where in the app the log call
originates. `DEBUG`-level logs are only emitted when `DEBUG=true`, and are
always suppressed when `ENV=prod`.

## Database roles and RLS

Tenant isolation is enforced by Postgres Row-Level Security (RLS), not by
"always remembering to filter by practice_id" in application code. That
only works if the connecting role is subject to RLS at all, which is why
there are **two distinct database roles**:

| Setting              | Role           | Used by                | RLS applies? |
|----------------------|----------------|-------------------------|--------------|
| `DATABASE_URL`       | `docflow`      | Alembic migrations only | No — owns the tables, and is a superuser in local dev |
| `APP_DATABASE_URL`   | `docflow_app`  | Application runtime queries (`app/db/session.py`) | Yes |

**This split is load-bearing, not stylistic.** In Postgres:
- The **table owner** bypasses RLS by default even with `ENABLE ROW LEVEL
  SECURITY` — the initial migration also sets `FORCE ROW LEVEL SECURITY`
  on every tenant-scoped table so ownership alone isn't an escape hatch.
- A **superuser** bypasses RLS unconditionally, `FORCE` or not. The
  Postgres Docker image's `POSTGRES_USER` (`docflow` here) is created as
  a superuser, so it can never be the role the application queries
  through.
- `docflow_app` is created by the initial migration as `NOSUPERUSER
  NOCREATEDB NOCREATEROLE NOBYPASSRLS`. **Any deployment must keep this
  role non-superuser and NOBYPASSRLS** — if it ever gains either
  attribute, every RLS policy below silently stops applying to it.

RLS-protected tables: `users`, `sessions`, `transcripts`, `notes`,
`consents`, `audit_logs`, `refresh_tokens` — every table carrying
`practice_id` (`practices` itself is the tenant root and isn't
RLS-scoped). Each has a single policy:

```sql
CREATE POLICY tenant_isolation_policy ON <table>
    USING (practice_id::text = current_setting('app.current_practice_id', true))
    WITH CHECK (practice_id::text = current_setting('app.current_practice_id', true));
```

`current_setting(..., true)` returns `NULL` (not an error) when the GUC
isn't set, and `practice_id::text = NULL` is never true — so the default,
with no tenant selected, is to see **zero rows**, not everyone's rows.

`app/db/session.py` provides `set_tenant(session, practice_id)`, which
issues `SET LOCAL app.current_practice_id = ...` for the current
transaction. **Per-request wiring** is done by
`app/auth/dependencies.py:get_tenant_session` — every route that depends
on `get_current_user` or `require_role(...)` (which both depend on
`get_tenant_session`) gets RLS scoped to the caller's own `practice_id`
automatically, derived from the verified access token's `practice_id`
claim, before any query runs. There is deliberately no way to obtain an
authenticated `User` without also getting a tenant-scoped session.

**One narrow, documented exception:** `POST /login` must look up a user
by email alone — before any tenant is known — which RLS's fail-closed
default makes impossible under the restricted role (no GUC set means zero
rows, including for a *real* user in some other tenant). `app/api/auth.py`
resolves this the same way the seed script does: a single read through
`app/db/session.py:get_admin_sessionmaker()` (the admin/superuser role),
scoped to exactly that one lookup. Every other query in the same request —
issuing tokens, writing the audit log — goes back through the tenant-scoped
app role once `practice_id` is known. This is the *only* place application
code (as opposed to Alembic migrations) uses the admin engine; anywhere
else it appears in a code review is a bug.

The dev role password (`docflow_app_dev_only`) is a hardcoded placeholder
in the initial migration, matching the `SECRET_KEY`/`JWT_SECRET`
placeholder pattern elsewhere in this repo. **Before any non-local
environment**, that password must be provisioned/rotated through a
secrets manager — never carried forward as a literal in a migration file.

## Immutable audit log

`audit_logs` rows must never change once written. This is enforced twice,
deliberately redundantly:

1. **App/ORM level** (`app/models/audit_log.py`): a SQLAlchemy
   `before_update`/`before_delete` event raises `AuditLogImmutableError`
   for any `AuditLog` instance, before the statement is even sent.
2. **Database level** (initial migration): `REVOKE UPDATE, DELETE ON
   audit_logs FROM docflow_app` — the app role can `INSERT` and `SELECT`
   but has no grant to alter or erase existing rows, full stop.

Neither is sufficient alone: (1) is bypassed by anything that talks to
Postgres directly (a script, `psql`, a different service using the same
role without going through the ORM); (2) is bypassed by anything running
as the admin/migration role. Together, a caller must control both the
application code path *and* hold admin DB credentials to alter history.

## Multi-tenancy quick reference for new tables/queries

- Any new PHI-bearing table: inherit `TenantMixin` (adds indexed,
  non-nullable `practice_id`), and add it to `TENANT_TABLES` in a new
  migration that enables + forces RLS and creates the same policy shape.
- Any new PHI text column: type it `PHIText` (`app/db/types.py`), not
  `Text` — this is the seam Phase 7's field-level encryption plugs into
  without a schema-churning migration.
- Any new runtime query path: use `app/db/session.py`'s engine/session
  (the `docflow_app` role), and call `set_tenant(...)` before querying
  tenant-scoped tables. Querying through the admin engine in application
  code is a bug, not just bad practice — it silently bypasses RLS.
