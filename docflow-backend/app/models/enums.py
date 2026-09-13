"""String-backed enums shared by the domain models.

Each is rendered as a native Postgres ENUM type by SQLAlchemy's Enum
column type (see the initial migration for the corresponding CREATE TYPE
statements).
"""

import enum


class PracticeStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class UserRole(str, enum.Enum):
    owner = "owner"
    clinician = "clinician"
    staff = "staff"


class SessionStatus(str, enum.Enum):
    created = "created"
    recording = "recording"
    transcribing = "transcribing"
    generating = "generating"
    complete = "complete"
    error = "error"
    # Phase 6: note generation failed (after retries) and the session was
    # degraded to just the transcript rather than losing the visit — see
    # app/services/note_service.py. Distinct from `complete` so a
    # clinician-facing session list can tell "note ready" apart from
    # "needs manual note writing" without joining notes.
    complete_degraded = "complete_degraded"


class NoteStatus(str, enum.Enum):
    draft = "draft"
    final = "final"


class ConsentType(str, enum.Enum):
    recording = "recording"
    retention = "retention"
    training = "training"


class AuditAction(str, enum.Enum):
    create = "create"
    read = "read"
    update = "update"
    delete = "delete"
    export = "export"
    # Auth events (Phase 3). Kept distinct from the CRUD actions above
    # rather than overloading e.g. "create" for login, so audit_logs stays
    # directly queryable by event type (`WHERE action = 'login_failure'`).
    register = "register"
    login_success = "login_success"
    login_failure = "login_failure"
    logout = "logout"
    mfa_enrolled = "mfa_enrolled"
    mfa_verified = "mfa_verified"
    token_reuse_detected = "token_reuse_detected"
    # Audio ingestion (Phase 5). Distinct from `create` because nothing is
    # created when a stream starts — kept as its own queryable event type.
    stream_started = "stream_started"
    # SOAP note generation (Phase 6). note.created reuses `create`
    # (resource_type="note"); note.degraded is kept distinct since it
    # marks a materially different outcome — a stub, not a real note.
    note_degraded = "note_degraded"
    # HIPAA controls (Phase 7): the consent gate (see
    # app/security/consent.py) denied retention for a transcript/note —
    # distinct from note_degraded (an LLM failure) since this is a
    # policy decision, not an error.
    retention_skipped = "retention_skipped"
    # Hardening/operations (Phase 8): one row per practice per scheduled
    # retention-purge run (app/worker/tasks.py's purge_expired_data_task)
    # — system-initiated (actor_user_id is None), metadata is PHI-free
    # counts only. Never emitted for audit_logs itself, which the purge
    # job never touches — see that module's docstring.
    purge_completed = "purge_completed"
