# docflow-backend

Backend skeleton for DocFlow.ai, an ambient AI medical scribe. This is the
Phase 1 project foundation only: config, structured/PHI-safe logging, health
endpoints, and dev/deploy tooling. Business routes, auth, models, and the
transcription/note pipeline land in later phases.

## Stack

- Python 3.12, FastAPI, Uvicorn
- `uv` for dependency management
- `pydantic-settings` for config
- Structured JSON logging with PHI redaction
- Ruff, mypy, pytest for dev tooling
- Docker + docker-compose (app, PostgreSQL 16, Redis 7)

## Local development

```bash
cp .env.example .env
make install
make run
```

The API is served at `http://localhost:8000`. Liveness: `GET /health`.
Readiness (checks Postgres + Redis): `GET /health/ready`.

## Common tasks

| Command         | Description                          |
|-----------------|---------------------------------------|
| `make install`  | Install dependencies via uv           |
| `make run`      | Run the dev server with reload        |
| `make test`     | Run the test suite with coverage      |
| `make lint`     | Run Ruff lint checks                  |
| `make format`   | Format code with Ruff                 |
| `make typecheck`| Run mypy                              |
| `make up`       | Start app + Postgres + Redis via Docker Compose |
| `make down`     | Stop the Docker Compose stack         |

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
