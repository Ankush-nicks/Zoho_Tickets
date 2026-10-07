"""
Routing log: one row per ticket - what the instructor picked on the Zoho
form, what the model picked (and how sure it was), what was decided
('agreed' / 'kept' the instructor's pick / 'overrode' it), the category the
ticket ended up in, and whether the routing turned out right - plus the
numbers for tuning config.REPORTER_OVERRIDE_MIN_CONFIDENCE from real
outcomes.

"Right or wrong" comes from corrections: a ticket corrected in this portal
or transferred in Zoho (both logged in the corrections table) was routed
wrong unless it was corrected back to the same category. A ticket nobody
corrected counts as right once it's out of review; one still waiting for
review or clarification is 'pending' and left out of the numbers.
"""
from .taxonomy import taxonomy

DECISIONS = ("agreed", "kept", "overrode")
_UNSETTLED = ("needs_human_review", "awaiting_clarification", "pending")
# Override thresholds the learning table tries.
THRESHOLDS = [round(0.5 + 0.05 * i, 2) for i in range(10)]  # 0.50 .. 0.95


def _leaf(leaf_id):
    return taxonomy.get(leaf_id) if leaf_id else None


def _name(leaf_id):
    leaf = _leaf(leaf_id)
    return leaf["name"] if leaf else leaf_id


def build_rows(tickets: list[dict], corrections: list[dict]) -> list[dict]:
    """Newest first. `corrections` oldest first, as db.list_corrections returns them."""
    by_ticket: dict[str, list[dict]] = {}
    for c in corrections:
        by_ticket.setdefault(c["ticket_id"], []).append(c)
    zoho_id = {t["id"]: t.get("zoho_ticket_id") for t in tickets}

    rows = []
    for t in tickets:
        fixes = by_ticket.get(t["id"], [])
        final = t.get("category_id")
        # Where our routing sent it: what the first correction corrected,
        # or - never corrected - where it still is.
        routed = fixes[0].get("predicted_category_id") if fixes else final
        if fixes:
            outcome = "right" if final == routed else "wrong"
        elif t.get("status") in _UNSETTLED:
            outcome = "pending"
        else:
            outcome = "right"
        final_leaf = _leaf(final)
        rows.append({
            "id": t["id"],
            "zoho_ticket_id": t.get("zoho_ticket_id"),
            "created_at": t.get("created_at"),
            "text": (t.get("original_text") or "")[:300],
            "status": t.get("status"),
            "reporter_category": t.get("reporter_category"),
            "reporter_subcategory": t.get("reporter_subcategory"),
            "reporter_leaf_id": t.get("reporter_leaf_id"),
            "model_category_id": t.get("model_category_id"),
            "model_category_name": _name(t.get("model_category_id")),
            "model_confidence": t.get("model_confidence"),
            "decision": t.get("decision"),
            "routed_category_id": routed,
            "routed_category_name": _name(routed),
            "final_category_id": final,
            "final_category_name": _name(final),
            "final_group_id": final_leaf["parent_id"] if final_leaf else None,
            "final_group_name": final_leaf["parent_name"] if final_leaf else None,
            "corrected": bool(fixes),
            "corrected_by": fixes[-1].get("corrected_by") if fixes else None,
            "outcome": outcome,
            "duplicate_of": t.get("duplicate_of"),
            "duplicate_of_zoho_id": zoho_id.get(t.get("duplicate_of")),
        })
    rows.sort(key=lambda r: r["created_at"] or 0, reverse=True)
    return rows


def _pct(part: int, whole: int):
    return round(100 * part / whole, 1) if whole else None


def learn(rows: list[dict], current_threshold: float) -> dict:
    """
    Step-6 numbers over the settled, non-duplicate rows that have a
    decision (the instructor picked a subcategory and the model ran):

    - per decision: how often the routing was right, how often the model's
      own pick was the final category, and how often the instructor's was;
    - for every disagreement (kept + overrode), which override threshold
      would have routed the most of them right: override when the model's
      confidence >= t, else keep the instructor's pick.
    """
    settled = [r for r in rows if r["decision"] in DECISIONS and not r["duplicate_of"] and r["outcome"] != "pending"]
    by_decision = {}
    for d in DECISIONS:
        rs = [r for r in settled if r["decision"] == d]
        model_right = sum(r["final_category_id"] == r["model_category_id"] for r in rs)
        instructor_right = sum(r["final_category_id"] == r["reporter_leaf_id"] for r in rs)
        routed_right = sum(r["outcome"] == "right" for r in rs)
        by_decision[d] = {
            "n": len(rs),
            "routed_right": routed_right, "routed_right_pct": _pct(routed_right, len(rs)),
            "model_right": model_right, "model_right_pct": _pct(model_right, len(rs)),
            "instructor_right": instructor_right, "instructor_right_pct": _pct(instructor_right, len(rs)),
        }

    disagreements = [
        r for r in settled
        if r["decision"] in ("kept", "overrode") and r["model_confidence"] is not None and r["reporter_leaf_id"]
    ]
    simulation = []
    for t in THRESHOLDS:
        right = sum(
            r["final_category_id"] == (r["model_category_id"] if r["model_confidence"] >= t else r["reporter_leaf_id"])
            for r in disagreements
        )
        overrides = sum(r["model_confidence"] >= t for r in disagreements)
        simulation.append({"threshold": t, "right": right, "overrides": overrides,
                           "right_pct": _pct(right, len(disagreements))})
    best = None
    if disagreements:
        # Highest accuracy; on a tie, the threshold nearest the current one
        # (no reason to move it for no gain).
        best = max(simulation, key=lambda s: (s["right"], -abs(s["threshold"] - current_threshold)))["threshold"]

    return {
        "settled": len(settled),
        "duplicates": sum(1 for r in rows if r["duplicate_of"]),
        "by_decision": by_decision,
        "disagreements": len(disagreements),
        "simulation": simulation,
        "current_threshold": current_threshold,
        "best_threshold": best,
    }
