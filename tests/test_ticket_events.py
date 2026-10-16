from app import db, ticket_events as te
from app.models import ClassificationResult

A = "G08-S02"  # Campus Network / Wi-Fi Issue
B = "G03-S04"  # Recording Issue
A_TXT = "Facilities & Equipment › Campus Network / Wi-Fi Issue"
B_TXT = "Instructor Platform (Teach OS) › Recording Issue (Platform / Software)"


def _result(decision, model_conf=0.9, final=A, conf=0.9):
    return ClassificationResult(
        category_id=final, confidence=conf, reasoning="r", needs_clarification=False,
        model_category_id=A, model_confidence=model_conf, decision=decision,
    )


def test_override_sentence_overrode():
    s = te.override_sentence(_result("overrode"), B, 0.9)
    assert s == (
        f"Model picked {A_TXT} (90%) over the instructor's {B_TXT}: "
        "at or above the 90% bar, so it replaced the pick."
    )


def test_override_sentence_kept():
    s = te.override_sentence(_result("kept", model_conf=0.72, final=B), B, 0.9)
    assert "(72%)" in s and "below the 90% bar" in s and "instructor's pick was kept" in s


def test_override_sentence_agreed_and_none():
    assert "agreed with the instructor's pick" in te.override_sentence(_result("agreed", final=B), B, 0.9)
    assert "No subcategory was picked by the instructor" in te.override_sentence(_result("none"), None, 0.9)
    assert "No subcategory was picked by the instructor" in te.override_sentence(_result(None), None, 0.9)


def test_presubmit_line_failure():
    s = te.presubmit_line(None, None, "Recording Issue", "G14-S01", True, 0.9)
    assert "Classification failed" in s and "fallback" in s


def test_presubmit_line_success():
    s = te.presubmit_line(_result("overrode"), B, None, A, False, 0.9)
    assert "replaced the pick" in s and A_TXT in s


def test_other_lines():
    assert B_TXT in te.on_add_line(True, B)
    assert te.pick_text(None) == "nothing" and te.pick_text("nope") == "nothing"
    assert A_TXT in te.transfer_line(B, A) and B_TXT in te.transfer_line(B, A)
    assert "Z-1" in te.duplicate_line("Z-1")
    assert te.edit_line("Open", "Closed")
    assert A_TXT in te.auto_classify_line(_result("none"), None, 0.9)


def test_add_and_list_events():
    tid = db.create_draft("hello", None)
    db.add_event(tid, "presubmit", "first")
    db.add_event(tid, "error", "second", error="boom", duration_ms=12)
    evs = db.list_events(tid)
    assert [e["kind"] for e in evs] == ["presubmit", "error"]
    assert evs[1]["error"] == "boom" and evs[1]["duration_ms"] == 12 and evs[1]["created_at"]
    assert db.event_flags()[tid] == {"events": 2, "errors": 1}
    assert [d["id"] for d in db.list_drafts()] == [tid]
