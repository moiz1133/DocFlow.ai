"""MockNoteGenerator: deterministic, no network, no keys.

The default whenever PHI_MODE=synthetic or no vendor is configured — see
app/notes/factory.py. Also the only generator exercised by the test
suite; app/notes/openai.py's module docstring explains why real vendor
calls never run in CI.

Constructor flags simulate the failure/retry paths a real vendor call can
take, mirroring app/transcription/mock.py's approach:
- simulate_malformed_once: fails validation on the first internal
  attempt, succeeds on the second — exercises the retry path.
- simulate_always_malformed: never succeeds — exercises the degradation
  path (see app/services/note_service.py).
- simulate_rate_limit / simulate_timeout: raise immediately, no retry —
  these are vendor-outage conditions, not malformed-output conditions.
"""

from app.notes.base import (
    NoteContext,
    NoteGenerator,
    NoteRateLimitError,
    NoteTimeoutError,
    NoteValidationError,
    SoapNote,
)

# A deterministic SOAP note summarizing the same synthetic primary-care
# visit app/transcription/mock.py's fixture transcript depicts (headache,
# fatigue, dehydration, disrupted sleep) — no real patient involved.
_FIXTURE_NOTE = SoapNote.compose(
    subjective=(
        "Patient reports a mild headache and fatigue for approximately three "
        "days. Denies fever, nausea, or vision changes. Reports disrupted "
        "sleep and reduced water intake over the same period."
    ),
    objective="Blood pressure check performed at this visit; other exam findings not stated.",
    assessment=(
        "Likely tension-type headache and fatigue secondary to poor sleep "
        "hygiene and reduced fluid intake."
    ),
    plan=(
        "Advised increased water intake and improved sleep hygiene. Blood "
        "pressure checked. Follow up if symptoms persist beyond one week or "
        "worsen."
    ),
    provider="mock",
    model=None,
)

# Internal bounded retry count for the simulated-malformed paths — a
# fixed, small constant (not read from Settings, unlike the real
# OpenAINoteGenerator's NOTE_MAX_RETRIES) since MockNoteGenerator's job is
# deterministic behavior for tests, not configurable retry tuning.
_MOCK_ATTEMPTS = 2


class MockNoteGenerator(NoteGenerator):
    provider_name = "mock"

    def __init__(
        self,
        *,
        simulate_malformed_once: bool = False,
        simulate_always_malformed: bool = False,
        simulate_rate_limit: bool = False,
        simulate_timeout: bool = False,
    ) -> None:
        self._simulate_malformed_once = simulate_malformed_once
        self._simulate_always_malformed = simulate_always_malformed
        self._simulate_rate_limit = simulate_rate_limit
        self._simulate_timeout = simulate_timeout

    async def generate_soap(self, transcript_text: str, context: NoteContext) -> SoapNote:
        if self._simulate_rate_limit:
            self.last_retry_count = 0
            raise NoteRateLimitError("mock: simulated rate limit exceeded")
        if self._simulate_timeout:
            self.last_retry_count = 0
            raise NoteTimeoutError("mock: simulated timeout")

        for attempt in range(_MOCK_ATTEMPTS):
            is_malformed = self._simulate_always_malformed or (
                self._simulate_malformed_once and attempt == 0
            )
            if not is_malformed:
                self.last_retry_count = attempt
                return _FIXTURE_NOTE

        self.last_retry_count = _MOCK_ATTEMPTS - 1
        raise NoteValidationError(
            f"mock: simulated malformed output persisted across {_MOCK_ATTEMPTS} attempts"
        )
