"""
Golden-set regression check for app/classifier.py's routing accuracy.

Ground truth: every human correction ever logged (app.db.list_corrections())
is a real (ticket text -> correct category) pair - a POC deliberately
overriding a wrong or uncertain AI answer, which is stronger ground truth
than scripts/compare_classifiers.py's loose match against Zoho's own
free-text category field.

Evaluated with leave-one-out few-shot retrieval: for each held-out
correction, the few-shot examples offered to the model are every OTHER
correction plus taxonomy.json's seed examples - never the correction being
tested itself, or its accuracy would be trivially inflated (the model
would just be shown its own answer as a "similar past example"). This
mirrors app/memory.py's real retrieve_similar() (same k, same
FEWSHOT_MIN_SIMILARITY floor) but computes every embedding once up front
and does the leave-one-out cosine-similarity search in plain numpy -
numpy is a transitive dependency of chromadb (already in requirements.txt),
not a new one. Never touches the app's real persistent Chroma store.

Reuses classifier._build_system_prompt()/_response_schema() directly so
the eval always reflects the actual production prompt, rather than a
copy that can silently drift out of sync with it.

Usage:
    OPENROUTER_API_KEY=sk-or-... OPENAI_API_KEY=sk-... python scripts/eval_classifier.py \
        [--min-corrections 5] [--limit 50] [--update-baseline]

Compares the resulting accuracy against scripts/.eval_baseline.json (if one
exists) and reports PASS / REGRESSION / IMPROVED. Run this after editing
taxonomy.json or swapping the classification model, before deploying, to
catch an accuracy drop the Stats tab's "Routing accuracy" section (built
from the same corrections table) would otherwise only surface days later
as corrections trickle in. Pass --update-baseline once you've reviewed a
change and want the new number to become the bar future runs compare
against.

--limit bounds the number of held-out examples actually sent to the
classify model (cost control) by taking the most recent N corrections;
the leave-one-out retrieval pool for each of them still includes every
other correction and every seed example, matching what real production
memory would have.

Respects whichever backend app.db is already configured for, same as
compare_classifiers.py.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from openai import OpenAI, RateLimitError  # noqa: E402

from app import classifier, config, db  # noqa: E402
from app.memory import _embed  # noqa: E402
from app.taxonomy import taxonomy  # noqa: E402

BASELINE_FILE = Path(__file__).resolve().parent / ".eval_baseline.json"
RESULTS_FILE = Path(__file__).resolve().parent / ".eval_results.jsonl"
REGRESSION_TOLERANCE = 0.05  # fraction (5 percentage points) allowed to drop before flagging


def latest_correction_per_ticket(corrections: list[dict]) -> list[dict]:
    """Keep only the most recent correction per ticket - if a ticket was
    corrected twice, only the final label is real ground truth (mirrors
    app/memory.py's add_example() dedup, which does the same for what
    actually ends up in production memory)."""
    latest: dict[str, dict] = {}
    for c in corrections:
        prior = latest.get(c["ticket_id"])
        if prior is None or c["created_at"] > prior["created_at"]:
            latest[c["ticket_id"]] = c
    return list(latest.values())


def build_golden_set() -> list[dict]:
    corrections = latest_correction_per_ticket(db.list_corrections())
    golden = []
    for c in corrections:
        ticket = db.get_ticket(c["ticket_id"])
        if not ticket or not ticket.get("full_context"):
            continue
        golden.append({
            "ticket_id": c["ticket_id"],
            "text": ticket["full_context"],
            "true_category_id": c["corrected_category_id"],
            "created_at": c["created_at"],
        })
    return golden


def cosine_sim_row(query: np.ndarray, pool: np.ndarray) -> np.ndarray:
    """Cosine similarity of one query vector against every row in pool."""
    query_n = query / (np.linalg.norm(query) + 1e-12)
    pool_n = pool / (np.linalg.norm(pool, axis=1, keepdims=True) + 1e-12)
    return pool_n @ query_n


def select_fewshot(sims: np.ndarray, pool_meta: list[dict], k: int, min_similarity: float) -> list[dict]:
    """Same selection rule as app/memory.py's retrieve_similar(): highest
    similarity first, dropping anything below the floor, capped at k."""
    order = np.argsort(-sims)
    fewshot = []
    for idx in order:
        if sims[idx] < min_similarity:
            break
        fewshot.append({"text": pool_meta[idx]["text"], "category_id": pool_meta[idx]["category_id"]})
        if len(fewshot) >= k:
            break
    return fewshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-corrections", type=int, default=5,
                         help="Refuse to run below this many golden examples - too few to mean anything.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only classify the most recent N corrections (cost control). Default: all.")
    parser.add_argument("--update-baseline", action="store_true",
                         help="Overwrite .eval_baseline.json with this run's accuracy.")
    args = parser.parse_args()

    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openrouter_key:
        print("Set OPENROUTER_API_KEY in the environment.")
        sys.exit(1)
    if not openai_key:
        print("Set OPENAI_API_KEY in the environment (needed for embeddings, not classification).")
        sys.exit(1)

    print(f"backend: {'Turso' if db.USE_TURSO else 'SQLite'}")
    db.init_db()

    golden = build_golden_set()
    print(f"{len(golden)} corrected tickets available as golden examples")
    if len(golden) < args.min_corrections:
        print(f"Fewer than --min-corrections ({args.min_corrections}) - not enough correction "
              f"history yet to run a meaningful eval. Keep correcting tickets and re-run later.")
        sys.exit(0)

    golden.sort(key=lambda g: g["created_at"])
    test_indices = list(range(len(golden)))
    if args.limit is not None and args.limit < len(golden):
        test_indices = test_indices[-args.limit:]  # most recent N

    seed_examples = taxonomy.seed_examples()
    all_texts = [e["text"] for e in seed_examples] + [g["text"] for g in golden]
    print(f"embedding {len(all_texts)} texts (one batch, not per held-out example)...")
    embeddings = np.array(_embed(all_texts, openai_key))

    n_seed = len(seed_examples)
    seed_embeddings = embeddings[:n_seed]
    golden_embeddings = embeddings[n_seed:]
    seed_meta = [{"text": e["text"], "category_id": e["category_id"]} for e in seed_examples]
    golden_meta = [{"text": g["text"], "category_id": g["true_category_id"]} for g in golden]

    client = OpenAI(api_key=openrouter_key, base_url=config.OPENROUTER_BASE_URL)
    predictions = []

    for n, i in enumerate(test_indices, 1):
        g = golden[i]
        pool_embeddings = np.vstack([seed_embeddings, np.delete(golden_embeddings, i, axis=0)])
        pool_meta = seed_meta + [m for j, m in enumerate(golden_meta) if j != i]

        sims = cosine_sim_row(golden_embeddings[i], pool_embeddings)
        fewshot = select_fewshot(sims, pool_meta, config.FEWSHOT_K, config.FEWSHOT_MIN_SIMILARITY)

        try:
            completion = client.chat.completions.create(
                model=config.OPENROUTER_CLASSIFY_MODEL,
                messages=[
                    {"role": "system", "content": classifier._build_system_prompt(fewshot)},
                    {"role": "user", "content": f"Ticket:\n{g['text']}"},
                ],
                response_format={"type": "json_schema", "json_schema": classifier._response_schema()},
                temperature=0,
            )
        except RateLimitError as e:
            print(f"[{n}/{len(test_indices)}] rate limited, stopping early: {e}")
            break

        raw = json.loads(completion.choices[0].message.content)
        predicted = raw["category_id"]
        is_correct = predicted == g["true_category_id"]
        row = {
            "ticket_id": g["ticket_id"],
            "true_category_id": g["true_category_id"],
            "predicted_category_id": predicted,
            "correct": is_correct,
        }
        predictions.append(row)
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[{n}/{len(test_indices)}] true={g['true_category_id']} predicted={predicted} "
              f"{'OK' if is_correct else 'MISS'} ({len(fewshot)} few-shot examples)")

    n = len(predictions)
    if n == 0:
        print("No predictions completed (rate-limited immediately?) - nothing to report.")
        return

    correct = sum(1 for p in predictions if p["correct"])
    accuracy = correct / n
    print(f"\n--- Accuracy: {accuracy * 100:.1f}% ({correct}/{n}) ---")

    misses = [p for p in predictions if not p["correct"]]
    if misses:
        confusion = Counter((p["true_category_id"], p["predicted_category_id"]) for p in misses)
        print("\nMost-confused pairs (true -> predicted):")
        for (true_id, pred_id), count in confusion.most_common(10):
            true_name = (taxonomy.get(true_id) or {}).get("name", true_id)
            pred_name = (taxonomy.get(pred_id) or {}).get("name", pred_id)
            print(f"  {count}x  {true_name}  ->  {pred_name}")

    if BASELINE_FILE.exists():
        baseline = json.loads(BASELINE_FILE.read_text())
        delta = accuracy - baseline["accuracy"]
        if delta < -REGRESSION_TOLERANCE:
            print(f"\nREGRESSION: {accuracy * 100:.1f}% vs baseline {baseline['accuracy'] * 100:.1f}% "
                  f"(n={baseline['n']}, recorded {baseline['recorded_at']}) - down {abs(delta) * 100:.1f} points.")
        elif delta > REGRESSION_TOLERANCE:
            print(f"\nIMPROVED: {accuracy * 100:.1f}% vs baseline {baseline['accuracy'] * 100:.1f}% "
                  f"- up {delta * 100:.1f} points.")
        else:
            print(f"\nWithin tolerance of baseline {baseline['accuracy'] * 100:.1f}%.")
    else:
        print("\nNo baseline recorded yet - run with --update-baseline to set one.")

    if args.update_baseline:
        BASELINE_FILE.write_text(json.dumps(
            {"accuracy": accuracy, "n": n, "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())},
            indent=2,
        ))
        print(f"Baseline updated to {accuracy * 100:.1f}% (n={n}).")


if __name__ == "__main__":
    main()
