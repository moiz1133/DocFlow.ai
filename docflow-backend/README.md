# docflow-backend

Backend for DocFlow.ai, an ambient AI medical scribe.

- **Phase 1** (done): project skeleton — config, structured/PHI-safe
  logging, health endpoints, dev/deploy tooling.
- **Phase 2** (done): data layer — SQLAlchemy 2.0 models, Alembic
  migrations, practice-scoped multi-tenancy enforced via Postgres
  Row-Level Security.

Auth logic, routes, and the transcription/note pipeline land in later
phases.

## Stack

- Python 3.12, FastAPI, Uvicorn
- `uv` for dependency management
- `pydantic-settings` for config
- Structured JSON logging with PHI redaction
- SQLAlchemy 2.0 (async, `Mapped[]` style) + Alembic, asyncpg driver
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
| `make migrate-up`  | Apply all Alembic migrations (`alembic upgrade head`)|
| `make migrate-down`| Roll back all migrations (`alembic downgrade base`)  |
| `make seed`        | Insert one dev practice + owner user (synthetic)     |
| `make test`        | Run the test suite with coverage (starts Postgres first) |
| `make lint`        | Run Ruff lint checks                                 |
| `make format`      | Format code with Ruff                                |
| `make typecheck`   | Run mypy                                             |
| `make up`          | Start app + Postgres + Redis via Docker Compose      |
| `make down`        | Stop the Docker Compose stack                        |

## Configuration

All configuration is environment-driven (`app/config.py`), loaded via
`pydantic-settings`. See `.env.example` for every setting. Notable ones:

- `ENV`: `dev` | `staging` | `prod`. If `ENV=prod`, `DEBUG` must be `False`.
- `PHI_MODE`: `synthetic` | `real`. Later phases must enforce that
  production never runs in `synthetic` mode, and that `real` mode is only
  enabled once a BAA'd PHI vendor stack is verified.

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
`consents`, `audit_logs` — every table carrying `practice_id`
(`practices` itself is the tenant root and isn't RLS-scoped). Each has a
single policy:

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
transaction. **Per-request wiring — deriving `practice_id` from the
authenticated user and calling this at the start of every request —
completes in Phase 3**; nothing in this phase's DB layer calls it
automatically.

The dev role password (`docflow_app_dev_only`) is a hardcoded placeholder
in the initial migration, matching the `SECRET_KEY` placeholder pattern
elsewhere in this repo. **Before any non-local environment**, that
password must be provisioned/rotated through a secrets manager — never
carried forward as a literal in a migration file.

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
