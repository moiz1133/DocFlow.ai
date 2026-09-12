"""Conservative, non-LLM chatter scrubber.

Removes clearly non-clinical lines (greetings, scheduling small talk,
off-topic asides) from a transcript BEFORE it reaches note generation.
Deliberately biased toward KEEPING a line when in doubt: a sentence is
only ever dropped when it matches a chatter pattern AND contains no
clinical-content marker — dropping a clinically relevant aside is worse
than leaving filler in. See tests/test_notes_scrub.py for a worked
example of that bias (a "by the way, ..." transition that would
otherwise read as chatter but is kept because it carries a clinical
detail).

The original Transcript row (Phase 5) is never touched — this only ever
operates on an in-memory copy of its text and returns a new string. See
app/services/note_service.py for where this sits in the pipeline and how
NOTE_SCRUB_ENABLED toggles it.
"""

import re
from abc import ABC, abstractmethod

# Phrase-level cues for non-clinical small talk. Deliberately narrow and
# literal (whole phrases, not single words like "weather" or "day" that
# would also appear in ordinary clinical sentences) — a false negative
# here just leaves filler in, which is the safe failure mode; a false
# positive could drop something clinically relevant.
_CHATTER_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bhow('s| is | was ) (the )?(weather|family|weekend|traffic)\b",
        r"\bnice (weather|to see you|seeing you)\b",
        r"\bhave a (great|good|nice) (day|weekend|one)\b",
        r"\bsee you (next time|soon|later)\b",
        r"\btake care\b",
        r"\bthanks for coming in\b",
        r"\bhow('s| have) (things|it) been\b",
        r"\bparking (was|is)\b",
        r"\bby the way\b",
        r"\bhow was your (drive|commute)\b",
    )
]

# If any of these appear in a sentence, it is kept regardless of whether
# it also matches a chatter pattern — the conservative override. Not
# exhaustive by design: it only needs to catch enough common clinical
# vocabulary that a chatter-flagged sentence carrying real content
# survives; anything not flagged as chatter in the first place is
# already kept.
_CLINICAL_MARKERS = re.compile(
    r"\b("
    r"pain|fever|headache|migraine|nausea|vomit\w*|dizz\w*|"
    r"blood pressure|heart rate|pulse|temperature|"
    r"medication|prescri\w*|dosage|dose|"
    r"symptom\w*|diagnos\w*|"
    r"chest|breath\w*|lung|cough\w*|"
    r"rash|swelling|swollen|bruis\w*|bleed\w*|injury|wound|"
    r"test|lab|x-ray|scan|"
    r"allerg\w*|infection"
    r")\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _is_chatter(sentence: str) -> bool:
    if _CLINICAL_MARKERS.search(sentence):
        return False
    return any(pattern.search(sentence) for pattern in _CHATTER_PATTERNS)


class Scrubber(ABC):
    """Interface for a transcript-cleaning pre-step. Left open for an
    LLM-based implementation later — callers only depend on this method.
    """

    @abstractmethod
    def scrub(self, transcript_text: str) -> str:
        raise NotImplementedError


class HeuristicScrubber(Scrubber):
    """Rule/keyword-based scrubber. No LLM, no network — see the module
    docstring for the conservative bias this implements.
    """

    def scrub(self, transcript_text: str) -> str:
        sentences = [s for s in _SENTENCE_SPLIT.split(transcript_text.strip()) if s]
        kept = [sentence for sentence in sentences if not _is_chatter(sentence)]
        return " ".join(kept)


__all__ = ["HeuristicScrubber", "Scrubber"]
