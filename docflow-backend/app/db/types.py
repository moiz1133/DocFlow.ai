"""PHI-bearing column type.

Every column that can hold protected health information (transcript text,
SOAP note text, a clinician's free-text patient reference, etc.) must use
`PHIText` instead of raw `Text`, even though today it behaves identically
to `Text`. This gives Phase 7 (field-level encryption) a single seam to
swap in an encrypting `TypeDecorator` implementation for every PHI column
at once, without touching call sites or running a schema-churning
migration for each affected table.
"""

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


class PHIText(TypeDecorator[str]):
    """Marker type for PHI-bearing text columns. Backed by plain Text for now."""

    impl = Text
    cache_ok = True
