# docflow-backend

Backend for DocFlow.ai, an ambient AI medical scribe.

- **Phase 1** (done): project skeleton — config, structured/PHI-safe
  logging, health endpoints, dev/deploy tooling.
- **Phase 2** (done): data layer — SQLAlchemy 2.0 models, Alembic
  migrations, practice-scoped multi-tenancy enforced via Postgres
  Row-Level Security.
- **Phase 3** (done): stateless JWT auth (access + refresh, MFA-ready,
  role-based access), per-request RLS wiring.

The transcription/note pipeline and full business routes land in later
phases.

## Stack

- Python 3.12, FastAPI, Uvicorn
- `uv` for dependency management
- `pydantic-settings` for config
- Structured JSON logging with PHI + auth-secret redaction
- SQLAlchemy 2.0 (async, `Mapped[]` style) + Alembic, asyncpg driver
- PyJWT, argon2-cffi (password hashing), pyotp (TOTP/MFA)
- Ruff, mypy, pytest for dev tooling
- Docker + docker-compose (app, PostgreSQL 16, Redis 7)

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

`/login` and `/refresh` are rate-limited per client IP via a Redis fixed
window (`app/auth/rate_limit.py`) — lenient today (30 requests/minute),
but the hook is real: tightening it is a config change, not new plumbing.

### Audit trail

`register`, `login_success`, `login_failure`, `logout`, `mfa_enrolled`,
`mfa_verified`, and `token_reuse_detected` are all written to `audit_logs`
(PHI-free metadata only — see "Immutable audit log" below). `login_failure`
is only logged when the email matched a real user (so there's a tenant to
attribute it to); an unknown-email attempt is covered by the rate limiter
instead, not an audit row, since there's no tenant to write one against.

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
