"""Import every model so Base.metadata is fully populated for Alembic."""

from app.models.audit_log import AuditLog
from app.models.consent import Consent
from app.models.encounter import EncounterSession
from app.models.note import Note
from app.models.practice import Practice
from app.models.refresh_token import RefreshToken
from app.models.transcript import Transcript
from app.models.user import User

__all__ = [
    "AuditLog",
    "Consent",
    "EncounterSession",
    "Note",
    "Practice",
    "RefreshToken",
    "Transcript",
    "User",
]
