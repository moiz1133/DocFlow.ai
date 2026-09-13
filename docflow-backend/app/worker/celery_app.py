"""The Celery application (Phase 8) — Redis broker + result backend, on
separate logical Redis DBs from REDIS_URL's own (see app/config.py's
CELERY_BROKER_URL/CELERY_RESULT_BACKEND defaults) so task traffic never
shares keyspace with rate-limit counters or health checks.

Run a worker with:
    uv run celery -A app.worker.celery_app worker --loglevel=info
and the scheduler (for purge_expired_data_task) with:
    uv run celery -A app.worker.celery_app beat --loglevel=info
See docker-compose.yml's `worker`/`beat` services and the Makefile's
`worker`/`beat` targets.

task_always_eager is left at Celery's normal default (False) here —
tests flip it on via a session-scoped autouse fixture in
tests/conftest.py, never by mutating this module's behavior for
non-test callers.
"""

from celery import Celery

from app.config import get_settings
from app.ops.durations import parse_duration


def _build_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "docflow",
        broker=settings.CELERY_BROKER_URL,
        backend=settings.CELERY_RESULT_BACKEND,
        include=["app.worker.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Task args are plain str(uuid)/dict only (see app/worker/tasks.py's
        # module docstring on why PHI never appears in a task arg) — no
        # pickle needed anywhere in this app.
        beat_schedule={
            "purge-expired-data": {
                "task": "app.worker.tasks.purge_expired_data_task",
                "schedule": parse_duration(settings.PURGE_INTERVAL).total_seconds(),
            },
        },
    )
    return app


celery_app = _build_celery_app()
