"""Unit tests for the conservative heuristic scrubber — see
app/notes/scrub.py. The DB-level guarantee ("the stored Transcript row
is never mutated") and the NOTE_SCRUB_ENABLED toggle are covered in
tests/test_note_service.py, since those need the full pipeline.
"""

from app.notes.scrub import HeuristicScrubber


def test_scrub_removes_chatter_but_preserves_a_clinical_aside() -> None:
    """The "by the way, ... swelling ..." sentence is deliberately built
    to match a chatter pattern ("by the way") while also carrying a
    clinical detail — this is the conservative-bias case: it must survive
    scrubbing even though a naive matcher would drop it.
    """
    raw = (
        "Good morning, how's the family doing? "
        "What brings you in today? "
        "I've had chest tightness and shortness of breath since yesterday. "
        "Nice weather we're having. "
        "By the way, I also noticed some swelling in my ankles. "
        "Great, let's check your blood pressure. "
        "Take care and see you next time."
    )

    cleaned = HeuristicScrubber().scrub(raw)
    lowered = cleaned.lower()

    # Pure chatter: dropped.
    assert "how's the family" not in lowered
    assert "nice weather" not in lowered
    assert "take care" not in lowered
    assert "see you next time" not in lowered

    # Clinically relevant, including the chatter-flagged aside: kept.
    assert "what brings you in today" in lowered
    assert "chest tightness" in cleaned
    assert "swelling in my ankles" in cleaned
    assert "blood pressure" in cleaned


def test_scrub_of_purely_clinical_text_is_a_no_op() -> None:
    raw = (
        "I've had a mild headache and some fatigue for about three days now. "
        "No fever that I've noticed, no nausea."
    )

    cleaned = HeuristicScrubber().scrub(raw)

    assert cleaned == raw


def test_scrub_of_empty_transcript_returns_empty_string() -> None:
    assert HeuristicScrubber().scrub("") == ""


def test_scrub_when_everything_is_chatter_returns_empty_string() -> None:
    raw = "Nice weather we're having. Take care and see you next time."
    assert HeuristicScrubber().scrub(raw) == ""
