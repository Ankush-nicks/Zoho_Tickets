"""
One-off accuracy comparison: OpenAI vs Gemini on the app's own classifier
pipeline (same taxonomy, same prompt, same few-shot retrieval - only the
final classification call differs). Not part of the running app; run this
manually whenever you want a read on whether switching (or dual-running)
models is worth it.

Ground truth: tickets that already carry a Zoho-provided category/sub-
category tag (from a live ticket's own "Category Of The Issue" fields) are
used as a rough reference via the same loose-match logic app/main.py uses
for its own Zoho-agreement indicator - not a hand-labeled eval set, so
treat the numbers as a signal, not a verdict.

Usage:
    OPENAI_API_KEY=sk-... GEMINI_API_KEY=... python scripts/compare_classifiers.py [--limit 20] [--results-file path.jsonl]

Resumable: each successful comparison is appended to --results-file
immediately (one JSON object per line) and skipped on future runs, so a
tightly rate-limited key can be run in small batches over time (e.g. a
cron job every ~30 min) and still build toward a larger sample. The
printed summary always covers every row ever accumulated in that file,
not just the current run's.

Respects whichever backend app.db is already configured for (FIREBASE_
CREDENTIALS_BASE64 set -> Firestore, unset -> local SQLite) - run this
with the same environment variables you'd run the app itself with.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import classifier, config, db  # noqa: E402
from app.taxonomy import taxonomy  # noqa: E402
from openai import RateLimitError as OpenAIRateLimitError  # noqa: E402

DEFAULT_RESULTS_FILE = str(Path(__file__).resolve().parent / ".compare_results.jsonl")


def _load_results(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _append_result(path: str, row: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _normalize_label(s: str) -> str:
    for ch in ("/", "-", "_"):
        s = s.replace(ch, " ")
    return " ".join(s.lower().split())


def _loosely_matches(predicted_name: str | None, zoho_tag: str | None) -> bool | None:
    """Mirrors app/main.py's _labels_loosely_match - same rough heuristic
    used for the portal's own Zoho-agreement indicator."""
    if not predicted_name or not zoho_tag:
        return None
    a, b = _normalize_label(predicted_name), _normalize_label(zoho_tag)
    return a == b or a in b or b in a


def _classify_safely(fn, *args):
    try:
        return fn(*args), None
    except OpenAIRateLimitError as e:
        return None, f"OpenAI rate limited: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    limit = 20
    results_file = DEFAULT_RESULTS_FILE
    args = sys.argv[1:]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--results-file" in args:
        results_file = args[args.index("--results-file") + 1]

    openai_key = os.environ.get("OPENAI_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not openai_key:
        print("Set OPENAI_API_KEY in the environment.")
        sys.exit(1)
    if not gemini_key:
        print("Set GEMINI_API_KEY in the environment.")
        sys.exit(1)

    print(f"backend: {'Firestore' if db.USE_FIRESTORE else 'SQLite'}")
    db.init_db()

    existing = _load_results(results_file)
    already_tested = {r["ticket_id"] for r in existing}
    print(f"{len(existing)} tickets already compared in {results_file}, skipping those")

    candidates = [
        t for t in db.list_all_tickets()
        if (t.get("full_context") or t.get("original_text"))
        and (t.get("zoho_category") or t.get("zoho_subcategory"))
        and (t.get("zoho_ticket_id") or t["id"]) not in already_tested
    ]
    sample = candidates[:limit]
    print(f"{len(candidates)} untested tickets have a Zoho category tag to compare against; testing {len(sample)}\n")

    new_rows = []
    for i, t in enumerate(sample, 1):
        text = (t.get("full_context") or t.get("original_text") or "").strip()
        zoho_tag = t.get("zoho_subcategory") or t.get("zoho_category")

        openai_result, openai_err = _classify_safely(classifier.classify, text, openai_key)
        gemini_result, gemini_err = _classify_safely(classifier.classify_gemini, text, gemini_key, openai_key)

        if openai_err:
            print(f"[{i}/{len(sample)}] OpenAI failed on {t['id']}: {openai_err}")
        if gemini_err:
            print(f"[{i}/{len(sample)}] Gemini failed on {t['id']}: {gemini_err}")
        if openai_err or gemini_err:
            # Stop rather than burn through the rest of the batch on a key
            # that's still rate-limited - the next scheduled run will retry
            # this same (untested) ticket since nothing was appended for it.
            break

        openai_name = (taxonomy.get(openai_result.category_id) or {}).get("name")
        gemini_name = (taxonomy.get(gemini_result.category_id) or {}).get("name")

        row = {
            "ticket_id": t.get("zoho_ticket_id") or t["id"],
            "zoho_tag": zoho_tag,
            "openai_category": openai_result.category_id,
            "openai_confidence": openai_result.confidence,
            "openai_matches_zoho": _loosely_matches(openai_name, zoho_tag),
            "gemini_category": gemini_result.category_id,
            "gemini_confidence": gemini_result.confidence,
            "gemini_matches_zoho": _loosely_matches(gemini_name, zoho_tag),
            "models_agree": openai_result.category_id == gemini_result.category_id,
        }
        new_rows.append(row)
        _append_result(results_file, row)
        print(
            f"[{i}/{len(sample)}] zoho='{zoho_tag}' | "
            f"openai={row['openai_category']} ({row['openai_confidence']:.2f}, "
            f"{'match' if row['openai_matches_zoho'] else 'no-match'}) | "
            f"gemini={row['gemini_category']} ({row['gemini_confidence']:.2f}, "
            f"{'match' if row['gemini_matches_zoho'] else 'no-match'}) | "
            f"{'AGREE' if row['models_agree'] else 'DIFFER'}"
        )

    rows = existing + new_rows
    if not rows:
        print("\nNo comparable results - nothing to summarize.")
        return

    n = len(rows)
    openai_zoho_matches = [r for r in rows if r["openai_matches_zoho"] is not None]
    gemini_zoho_matches = [r for r in rows if r["gemini_matches_zoho"] is not None]

    def rate(matches, key):
        return sum(1 for r in matches if r[key]) / len(matches) * 100 if matches else float("nan")

    print(f"\n--- Cumulative summary over {n} compared tickets ({len(new_rows)} new this run) ---")
    print(f"OpenAI agrees with Zoho's own tag: {rate(openai_zoho_matches, 'openai_matches_zoho'):.0f}% "
          f"({len(openai_zoho_matches)} tickets had a comparable tag)")
    print(f"Gemini agrees with Zoho's own tag: {rate(gemini_zoho_matches, 'gemini_matches_zoho'):.0f}% "
          f"({len(gemini_zoho_matches)} tickets had a comparable tag)")
    print(f"OpenAI and Gemini agree with each other: {sum(1 for r in rows if r['models_agree'])}/{n} "
          f"({sum(1 for r in rows if r['models_agree'])/n*100:.0f}%)")
    print(f"Average confidence - OpenAI: {sum(r['openai_confidence'] for r in rows)/n:.2f}, "
          f"Gemini: {sum(r['gemini_confidence'] for r in rows)/n:.2f}")


if __name__ == "__main__":
    main()
