"""Celery tasks (Phase 8): async SOAP note generation and the scheduled
retention-purge job.

TENANT CONTEXT: a worker process has no FastAPI request to inherit RLS
scoping from, so every DB access in this module re-establishes it
itself via app.services.transcription_service.tenant_session (the same
helper the WS handler and note_service already use for the identical
reason) — never a bare, unscoped session.

TASK ARGS: session_id/practice_id/actor_user_id are passed as plain
strings (uuid.UUID isn't JSON-serializable), never PHI — task args are
stored/logged by the Celery result backend, so the same "never put PHI
where infrastructure might log or persist it" rule that governs
app/models/audit_log.py governs this boundary too.

generate_note_task wraps note_service.generate_note_for_session with NO
rewrite of that function (see its own docstring — this was the point of
keeping it a plain async callable back in Phase 6/7): the task is a thin
sync-Celery-task -> asyncio.run bridge around it, plus the pieces a
request handler doesn't need but a worker does — idempotency-on-retry
and marking the session `error` on a genuine (non-vendor) failure.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_admin_sessionmaker
from app.models import EncounterSession, Note, Practice, RefreshToken, Transcript
from app.models.enums import AuditAction, SessionStatus
from app.ops.durations import parse_duration
from app.ops.metrics import (
    CELERY_TASK_DURATION_SECONDS,
    CELERY_TASK_TOTAL,
    PURGE_ROWS_DELETED_TOTAL,
)
from app.services.audit_service import RequestMeta, record_phi_access
from app.services.note_service import (
    SessionNotFoundError,
    TranscriptNotReadyError,
    generate_note_for_session,
)
from app.services.transcription_service import tenant_session
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _with_engine_cleanup[T](coro: Coroutine[Any, Any, T]) -> T:
    """Disposes both @lru_cache process-lifetime DB engines after `coro`
    finishes, INSIDE the same loop that ran it (dispose() is itself
    async and must run before that loop closes) — see _run_async's
    docstring for why a Celery worker needs this on every task.
    """
    try:
        return await coro
    finally:
        from app.db.session import get_admin_engine, get_engine

        await get_engine().dispose()
        await get_admin_engine().dispose()


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Runs `coro` to completion from this SYNC Celery task body.

    A real Celery worker process never has an event loop running when a
    task starts (prefork/solo/threads pools all invoke the task on a
    plain thread with no loop) — asyncio.run() is exactly right there,
    and is the only branch that ever executes in production. It creates
    a BRAND NEW loop every call, though, and app/db/session.py's engines
    are @lru_cache PROCESS-lifetime singletons whose pooled asyncpg
    connections stay bound to whichever loop first used them — without
    cleanup, the pool would hand a *later* task's new loop a connection
    left over from an *earlier* task's now-closed loop (confirmed
    empirically in a real Celery worker: asyncpg raises "Future attached
    to a different loop" on the second task through the same forked
    worker process). _with_engine_cleanup disposes both engines before
    this loop closes, so the next task's asyncio.run() call always
    starts from a clean pool — every task pays one fresh-connection cost
    in exchange for correctness, an acceptable tradeoff at this
    workload's scale (one task per clinical visit / one purge run per
    PURGE_INTERVAL, not a tight per-millisecond loop).

    The other branch exists solely for `task_always_eager` (see
    tests/conftest.py's _celery_eager_mode): calling .delay() from an
    async test means Celery invokes this task function directly, still
    on the SAME thread and SAME already-running event loop as the test
    itself — asyncio.run() would raise ("cannot be called from a
    running event loop"), and running the coroutine on a *different*
    loop (e.g. a new thread) is not a safe alternative here either, for
    the identical reason: the engines already hold connections bound to
    the test's own session-scoped loop. nest_asyncio patches the
    CURRENTLY RUNNING loop to tolerate a nested run instead, so the
    coroutine executes on that SAME loop — no engine disposal needed
    here, since there's no loop teardown between tasks within one test
    session.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_with_engine_cleanup(coro))

    import nest_asyncio

    nest_asyncio.apply()
    return asyncio.get_event_loop().run_until_complete(coro)


# "Orphaned" for purge purposes: a session that never reached a
# note-generation terminal state and has gone stale — created/recording
# (client vanished before ever finalizing) or error (a transcription/
# limit failure). Deliberately excludes transcribing/generating: those
# are actively in flight from this process's own point of view, and
# deleting one out from under an in-progress request/task would be a bug,
# not a cleanup.
_ORPHAN_SESSION_STATUSES = (SessionStatus.created, SessionStatus.recording, SessionStatus.error)


# ---------------------------------------------------------------------------
# Async note generation
# ---------------------------------------------------------------------------


@celery_app.task(  # type: ignore[untyped-decorator]  # celery has no type stubs — see pyproject.toml
    bind=True,
    name="app.worker.tasks.generate_note_task",
    # TranscriptNotReadyError can legitimately be transient (a rare race
    # against the finalize/WS-persist path committing its transcript) —
    # bounded, jittered retry gives that race a chance to resolve itself.
    # SessionNotFoundError is NOT retried: the route that enqueues this
    # task already confirmed the session exists moments earlier, so this
    # can only mean the session was deleted out from under the task,
    # which a retry cannot fix.
    autoretry_for=(TranscriptNotReadyError,),
    retry_backoff=True,
    retry_backoff_max=2,
    retry_jitter=True,
    max_retries=3,
)
def generate_note_task(
    self: object,
    session_id: str,
    practice_id: str,
    actor_user_id: str,
    request_meta: RequestMeta | None = None,
) -> dict[str, str]:
    return _run_async(
        _generate_note_async(
            uuid.UUID(session_id),
            uuid.UUID(practice_id),
            uuid.UUID(actor_user_id),
            request_meta,
        )
    )


async def _note_already_generated(practice_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    async with tenant_session(practice_id) as db:
        result = await db.execute(select(Note.id).where(Note.session_id == session_id))
        return result.first() is not None


async def _mark_session_error(practice_id: uuid.UUID, session_id: uuid.UUID) -> None:
    async with tenant_session(practice_id) as db:
        encounter = await db.get(EncounterSession, session_id)
        if encounter is not None:
            encounter.status = SessionStatus.error
            await db.flush()


async def _generate_note_async(
    session_id: uuid.UUID,
    practice_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_meta: RequestMeta | None,
) -> dict[str, str]:
    # IDEMPOTENCY: a Note row already existing for this session means a
    # prior run of this task already completed successfully (whether
    # this is a Celery-driven retry after a late-stage transient failure,
    # or a duplicate enqueue) — skip regenerating rather than creating a
    # second Note row / re-running vendor calls.
    if await _note_already_generated(practice_id, session_id):
        logger.info(
            "generate_note_task: note already exists, skipping duplicate run",
            extra={"outcome": "skipped_duplicate"},
        )
        CELERY_TASK_TOTAL.labels(task="generate_note", outcome="skipped_duplicate").inc()
        return {"outcome": "skipped_duplicate"}

    CELERY_TASK_TOTAL.labels(task="generate_note", outcome="started").inc()
    started_at = time.monotonic()
    try:
        result = await generate_note_for_session(
            session_id=session_id,
            practice_id=practice_id,
            actor_user_id=actor_user_id,
            request_meta=request_meta,
        )
    except TranscriptNotReadyError:
        raise  # retried by Celery — see the task decorator's autoretry_for
    except SessionNotFoundError:
        logger.error(
            "generate_note_task: session not found, not retrying", extra={"outcome": "failed"}
        )
        CELERY_TASK_TOTAL.labels(task="generate_note", outcome="failed").inc()
        raise
    except Exception:
        # Anything else is a genuine infra/unexpected failure, not a
        # vendor error (those are already caught and degraded-persisted
        # inside generate_note_for_session — see its DEGRADATION POLICY).
        logger.exception("generate_note_task: unexpected failure")
        await _mark_session_error(practice_id, session_id)
        CELERY_TASK_TOTAL.labels(task="generate_note", outcome="failed").inc()
        raise
    finally:
        CELERY_TASK_DURATION_SECONDS.labels(task="generate_note").observe(
            time.monotonic() - started_at
        )

    outcome = "degraded" if result.degraded else "success"
    CELERY_TASK_TOTAL.labels(task="generate_note", outcome=outcome).inc()
    return {"outcome": outcome, "note_id": str(result.note.id)}


# ---------------------------------------------------------------------------
# Scheduled retention purge
# ---------------------------------------------------------------------------


@celery_app.task(name="app.worker.tasks.purge_expired_data_task")  # type: ignore[untyped-decorator]
def purge_expired_data_task() -> dict[str, object]:
    return _run_async(_purge_expired_data_async())


async def _purge_practice(
    db: AsyncSession,
    *,
    practice_id: uuid.UUID,
    cutoff_unretained: datetime,
    cutoff_orphan: datetime,
    now: datetime,
    dry_run: bool,
) -> dict[str, int]:
    counts = {
        "transcripts_deleted": 0,
        "notes_deleted": 0,
        "sessions_deleted": 0,
        "refresh_tokens_deleted": 0,
    }

    transcript_ids = (
        (
            await db.execute(
                select(Transcript.id).where(
                    Transcript.is_retained.is_(False),
                    Transcript.updated_at < cutoff_unretained,
                )
            )
        )
        .scalars()
        .all()
    )
    counts["transcripts_deleted"] = len(transcript_ids)
    if transcript_ids and not dry_run:
        await db.execute(delete(Transcript).where(Transcript.id.in_(transcript_ids)))

    note_ids = (
        (
            await db.execute(
                select(Note.id).where(
                    Note.is_retained.is_(False), Note.updated_at < cutoff_unretained
                )
            )
        )
        .scalars()
        .all()
    )
    counts["notes_deleted"] = len(note_ids)
    if note_ids and not dry_run:
        await db.execute(delete(Note).where(Note.id.in_(note_ids)))

    # Orphaned/stale sessions — see _ORPHAN_SESSION_STATUSES. Defensively
    # excludes any session that still has a RETAINED transcript or note:
    # Transcript/Note both CASCADE-delete with their session (see the
    # initial migration's ondelete="CASCADE"), so without this check a
    # session purge could silently take retained clinical content with
    # it — that would be a compliance bug, not a cleanup, regardless of
    # how unlikely an error/created session with retained content is in
    # practice.
    session_ids = (
        (
            await db.execute(
                select(EncounterSession.id).where(
                    EncounterSession.status.in_(_ORPHAN_SESSION_STATUSES),
                    EncounterSession.updated_at < cutoff_orphan,
                    ~exists(
                        select(Transcript.id).where(
                            Transcript.session_id == EncounterSession.id,
                            Transcript.is_retained.is_(True),
                        )
                    ),
                    ~exists(
                        select(Note.id).where(
                            Note.session_id == EncounterSession.id, Note.is_retained.is_(True)
                        )
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    counts["sessions_deleted"] = len(session_ids)
    if session_ids and not dry_run:
        await db.execute(delete(EncounterSession).where(EncounterSession.id.in_(session_ids)))

    # Expired refresh tokens — deliberately `expires_at < now` only, NOT
    # `revoked.is_(True)`: a revoked-but-not-yet-expired row is exactly
    # what app/auth/refresh_store.py's reuse detection checks against
    # (rotate_refresh_token raises RefreshTokenReuseDetected when a
    # revoked jti is presented again). Purging it the moment it's revoked
    # would let a replayed stolen token look like "unknown token" instead
    # of triggering reuse detection — a real security regression. Only
    # once a token would have expired anyway is it safe to remove.
    token_ids = (
        (await db.execute(select(RefreshToken.id).where(RefreshToken.expires_at < now)))
        .scalars()
        .all()
    )
    counts["refresh_tokens_deleted"] = len(token_ids)
    if token_ids and not dry_run:
        await db.execute(delete(RefreshToken).where(RefreshToken.id.in_(token_ids)))

    # One PHI-free audit row per practice per run — counts only, never
    # resource ids or content. actor_user_id=None: this is a
    # system-initiated event, not a user action (see
    # app/models/audit_log.py's docstring on nullable actor_user_id).
    await record_phi_access(
        db,
        practice_id=practice_id,
        actor_user_id=None,
        action=AuditAction.purge_completed,
        resource_type="purge",
        resource_id=None,
        metadata={"dry_run": dry_run, **counts},
    )

    return counts


async def _purge_expired_data_async() -> dict[str, object]:
    settings = get_settings()
    dry_run = settings.PURGE_DRY_RUN
    now = datetime.now(UTC)
    cutoff_unretained = now - parse_duration(settings.PURGE_UNRETAINED_AFTER)
    cutoff_orphan = now - parse_duration(settings.PURGE_ORPHAN_SESSION_AFTER)

    summary = {
        "transcripts_deleted": 0,
        "notes_deleted": 0,
        "sessions_deleted": 0,
        "refresh_tokens_deleted": 0,
    }

    CELERY_TASK_TOTAL.labels(task="purge_expired_data", outcome="started").inc()
    started_at = time.monotonic()
    try:
        # The admin/migration role is used here ONLY to enumerate which
        # practices exist (a cross-tenant listing, same narrow exception
        # documented on app/db/session.py's get_admin_engine) — every
        # actual delete below runs through tenant_session, RLS-scoped to
        # one practice at a time, exactly like every other write path in
        # this codebase. No query in this module ever bypasses RLS for
        # PHI-bearing rows.
        async with get_admin_sessionmaker()() as admin_db:
            practice_ids = (await admin_db.execute(select(Practice.id))).scalars().all()

        for practice_id in practice_ids:
            async with tenant_session(practice_id) as db:
                counts = await _purge_practice(
                    db,
                    practice_id=practice_id,
                    cutoff_unretained=cutoff_unretained,
                    cutoff_orphan=cutoff_orphan,
                    now=now,
                    dry_run=dry_run,
                )
            for key, value in counts.items():
                summary[key] += value
    except Exception:
        logger.exception("purge_expired_data_task: unexpected failure")
        CELERY_TASK_TOTAL.labels(task="purge_expired_data", outcome="failed").inc()
        raise
    finally:
        CELERY_TASK_DURATION_SECONDS.labels(task="purge_expired_data").observe(
            time.monotonic() - started_at
        )

    for resource_type, count in summary.items():
        if count:
            PURGE_ROWS_DELETED_TOTAL.labels(resource_type=resource_type).inc(count)

    # audit_logs is never purged — this task has no code path that
    # touches the AuditLog model at all (only Transcript/Note/
    # EncounterSession/RefreshToken above), so there is nothing to
    # assert against beyond that absence. See app/models/audit_log.py:
    # the table is also immutable/undeletable by the app DB role
    # regardless (REVOKE UPDATE, DELETE), so even a future bug here
    # could not actually delete an audit row.
    CELERY_TASK_TOTAL.labels(task="purge_expired_data", outcome="success").inc()
    logger.info("purge run complete", extra={"dry_run": dry_run, **summary})
    return {"dry_run": dry_run, **summary}
