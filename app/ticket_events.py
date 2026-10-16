"""Plain-English, one-line descriptions of what happened on a ticket, for the
Log page's per-ticket timeline. Pure functions: no database or network."""
from .taxonomy import taxonomy
from .models import ClassificationResult

KINDS = ("presubmit", "on_add", "edit", "transfer", "duplicate", "auto_classify", "error")


def _pct(c: float) -> str:
    return f"{round(c * 100)}%"


def pick_text(leaf_id: str | None) -> str:
    """'Group › Sub', or 'nothing' when the id is missing or unknown."""
    leaf = taxonomy.get(leaf_id) if leaf_id else None
    if not leaf:
        return "nothing"
    return f"{leaf['parent_name']} › {leaf['name']}"


def override_sentence(result: ClassificationResult, instructor_leaf_id: str | None, threshold: float) -> str:
    bar = _pct(threshold)
    instructor = pick_text(instructor_leaf_id)
    model = pick_text(result.model_category_id or result.category_id)
    model_conf = result.model_confidence if result.model_confidence is not None else result.confidence
    if result.decision == "overrode":
        return (f"Model picked {model} ({_pct(model_conf)}) over the instructor's {instructor}: "
                f"at or above the {bar} bar, so it replaced the pick.")
    if result.decision == "kept":
        return (f"Model picked {model} ({_pct(model_conf)}) over the instructor's {instructor}: "
                f"below the {bar} bar, so the instructor's pick was kept.")
    if result.decision == "agreed":
        return f"The model agreed with the instructor's pick, {instructor} ({_pct(model_conf)})."
    return "No subcategory was picked by the instructor, so the model's choice stands."


def presubmit_line(result: ClassificationResult | None, instructor_leaf_id: str | None,
                   instructor_pick: str | None, sent_leaf_id: str, used_fallback: bool,
                   threshold: float) -> str:
    if result is None:
        pick = pick_text(instructor_leaf_id) if instructor_leaf_id else (instructor_pick or "nothing")
        line = f"Classification failed (instructor picked {pick})."
        if used_fallback:
            line += f" Sent the fallback subcategory {pick_text(sent_leaf_id)} to Zoho."
        return line
    return (f"Suggested {pick_text(sent_leaf_id)} ({_pct(result.confidence)}). "
            f"{override_sentence(result, instructor_leaf_id, threshold)}")


def on_add_line(adopted: bool, final_leaf_id: str | None) -> str:
    if adopted:
        return f"Ticket created in Zoho with the suggested subcategory {pick_text(final_leaf_id)}."
    return f"Ticket created in Zoho; its subcategory is {pick_text(final_leaf_id)}."


def edit_line(old_status: str | None, new_status: str | None) -> str:
    return f"Status changed from {old_status or 'nothing'} to {new_status or 'nothing'}."


def transfer_line(from_leaf_id: str | None, to_leaf_id: str) -> str:
    return f"Ticket moved from {pick_text(from_leaf_id)} to {pick_text(to_leaf_id)}."


def duplicate_line(first_zoho_id: str | None) -> str:
    if first_zoho_id:
        return f"Duplicate of ticket {first_zoho_id}, counted once in accuracy."
    return "Duplicate of an earlier ticket, counted once in accuracy."


def auto_classify_line(result: ClassificationResult, instructor_leaf_id: str | None, threshold: float) -> str:
    return (f"Auto-classified as {pick_text(result.category_id)} ({_pct(result.confidence)}). "
            f"{override_sentence(result, instructor_leaf_id, threshold)}")
