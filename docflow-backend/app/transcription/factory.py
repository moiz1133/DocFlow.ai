"""Vendor selection for the transcription layer.

`build_transcriber(settings)` is the pure, unmemoized selection logic —
easy to call with a custom Settings in tests. `get_transcriber()` is the
process-wide singleton wrapper actual application code should use,
matching the zero-arg `@lru_cache` convention already established by
app/db/session.py's get_engine()/get_sessionmaker().

The `openai` module is imported lazily, only inside the "openai" branch —
selecting vendor="mock" (the default, and the only option when
PHI_MODE=synthetic) never touches the SDK at all. See
tests/test_transcription_isolation.py.
"""

import logging
from functools import lru_cache

from app.config import Settings, get_settings
from app.transcription.base import Transcriber
from app.transcription.mock import MockTranscriber

logger = logging.getLogger(__name__)


class TranscriberConfigurationError(Exception):
    """Raised when TRANSCRIBER_VENDOR is misconfigured for the current
    environment — always meant to fail application startup, not be caught
    and worked around at request time.
    """


def build_transcriber(settings: Settings) -> Transcriber:
    vendor = settings.TRANSCRIBER_VENDOR

    # Real vendors only ever handle real, BAA-covered audio. Synthetic
    # data (dev/test) must never leave the process, regardless of what
    # TRANSCRIBER_VENDOR is set to — this is not overridable.
    if settings.PHI_MODE == "synthetic":
        vendor = "mock"

    if settings.ENV == "prod" and vendor == "mock":
        raise TranscriberConfigurationError(
            "TRANSCRIBER_VENDOR resolved to 'mock' with ENV=prod — a real "
            "vendor is required in production. If PHI_MODE=synthetic is "
            "forcing this, that combination is itself invalid in prod."
        )

    if vendor == "mock":
        return MockTranscriber()

    if vendor == "openai":
        if not settings.OPENAI_API_KEY:
            raise TranscriberConfigurationError(
                "TRANSCRIBER_VENDOR=openai requires OPENAI_API_KEY to be set"
            )
        # NOTE: real (non-synthetic) audio must not reach OpenAI without a
        # signed BAA *and* a Zero Data Retention agreement in place — this
        # factory has no way to verify that operationally, so it is a
        # deployment/process precondition, not a check this code can make.
        from app.transcription.openai import OpenAITranscriber

        return OpenAITranscriber(settings)

    raise TranscriberConfigurationError(f"Unknown TRANSCRIBER_VENDOR: {vendor!r}")


@lru_cache
def get_transcriber() -> Transcriber:
    transcriber = build_transcriber(get_settings())
    logger.info("transcriber selected", extra={"provider": transcriber.provider_name})
    return transcriber
