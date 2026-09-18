"""
Unit tests for the pure/offline logic in scripts/eval_classifier.py - the
leave-one-out golden-set regression check. Doesn't exercise main() itself
(that needs real API keys and hits OpenRouter/OpenAI - see the module
docstring), just the dedup and similarity-selection helpers that make the
eval statistically honest (no data leakage from a correction into its own
few-shot context).
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "eval_classifier.py"
spec = importlib.util.spec_from_file_location("eval_classifier", SCRIPT_PATH)
eval_classifier = importlib.util.module_from_spec(spec)
sys.modules["eval_classifier"] = eval_classifier
spec.loader.exec_module(eval_classifier)


def test_latest_correction_per_ticket_keeps_only_the_newest():
    corrections = [
        {"ticket_id": "t1", "created_at": 1.0, "corrected_category_id": "a"},
        {"ticket_id": "t1", "created_at": 5.0, "corrected_category_id": "b"},  # re-corrected, newer
        {"ticket_id": "t2", "created_at": 3.0, "corrected_category_id": "c"},
    ]

    result = {r["ticket_id"]: r for r in eval_classifier.latest_correction_per_ticket(corrections)}

    assert len(result) == 2
    assert result["t1"]["corrected_category_id"] == "b"
    assert result["t2"]["corrected_category_id"] == "c"


def test_latest_correction_per_ticket_order_independent():
    """Same as above but with rows in the opposite order - the comparison
    must be by created_at, not by list position."""
    corrections = [
        {"ticket_id": "t1", "created_at": 5.0, "corrected_category_id": "b"},
        {"ticket_id": "t1", "created_at": 1.0, "corrected_category_id": "a"},
    ]

    result = eval_classifier.latest_correction_per_ticket(corrections)

    assert len(result) == 1
    assert result[0]["corrected_category_id"] == "b"


def test_cosine_sim_row_identical_vector_is_one():
    query = np.array([1.0, 0.0])
    pool = np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]])

    sims = eval_classifier.cosine_sim_row(query, pool)

    assert sims[0] == pytest.approx(1.0)
    assert sims[1] == pytest.approx(0.0)
    assert sims[2] == pytest.approx(-1.0)


def test_select_fewshot_drops_below_floor_and_caps_at_k():
    pool_meta = [
        {"text": "a", "category_id": "cat-a"},
        {"text": "b", "category_id": "cat-b"},
        {"text": "c", "category_id": "cat-c"},
    ]
    sims = np.array([0.9, 0.05, 0.6])  # b is below the floor

    fewshot = eval_classifier.select_fewshot(sims, pool_meta, k=5, min_similarity=0.15)

    assert [f["category_id"] for f in fewshot] == ["cat-a", "cat-c"]  # highest similarity first


def test_select_fewshot_respects_k_even_when_all_pass_floor():
    pool_meta = [{"text": str(i), "category_id": f"cat-{i}"} for i in range(5)]
    sims = np.array([0.9, 0.8, 0.7, 0.6, 0.5])

    fewshot = eval_classifier.select_fewshot(sims, pool_meta, k=2, min_similarity=0.0)

    assert len(fewshot) == 2
    assert [f["category_id"] for f in fewshot] == ["cat-0", "cat-1"]
