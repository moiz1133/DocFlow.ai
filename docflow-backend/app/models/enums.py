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
