"""
Drafts a small definition change after a classification correction: one line
for the subcategory the issue belongs in, one for the subcategory it was
wrongly sent to. apply_lines() applies such lines to a taxonomy dict.
"""
import json

from openai import OpenAI

from . import config
from .taxonomy import taxonomy

RIGHT_PREFIX = "Also route here when"
WRONG_PREFIX = "NOT this: "


def _schema() -> dict:
    return {
        "name": "definition_change",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "right_line": {
                    "type": "string",
                    "description": "One sentence for the correct subcategory's description, starting 'Also route here when'.",
                },
                "wrong_line": {
                    "type": "string",
                    "description": "One short clause for the wrongly-chosen subcategory's description saying what does NOT belong there.",
                },
            },
            "required": ["right_line", "wrong_line"],
            "additionalProperties": False,
        },
    }


def _normalise(right_line: str, wrong_line: str, right: dict) -> dict:
    right_line = " ".join((right_line or "").split())
    if not right_line.startswith(RIGHT_PREFIX):
        right_line = f"{RIGHT_PREFIX} {right_line}".rstrip()
    wrong_line = " ".join((wrong_line or "").split())
    if wrong_line.startswith(WRONG_PREFIX.strip()):
        wrong_line = wrong_line[len(WRONG_PREFIX.strip()):].strip()
    suffix = f" -> {right['parent_name']} > {right['name']}."
    wrong_line = wrong_line.rstrip()
    if wrong_line.endswith(suffix):
        wrong_line = wrong_line[: -len(suffix)]
    wrong_line = wrong_line.rstrip(" .")
    return {"right_line": right_line, "wrong_line": f"{WRONG_PREFIX}{wrong_line}{suffix}"}


def draft_lines(issue_text: str, wrong_leaf_id: str, right_leaf_id: str, api_key: str) -> dict:
    """One model call; returns {"right_line", "wrong_line"} in the house format."""
    wrong, right = taxonomy.get(wrong_leaf_id), taxonomy.get(right_leaf_id)
    if not wrong or not right:
        raise ValueError("unknown subcategory")
    prompt = f"""A support ticket was routed to the wrong subcategory. Draft a minimal definition change so
similar tickets route correctly next time.

TICKET:
{issue_text}

CORRECT subcategory: {right['parent_name']} > {right['name']}
Current description: {right.get('description', '')}

WRONGLY CHOSEN subcategory: {wrong['parent_name']} > {wrong['name']}
Current description: {wrong.get('description', '')}

Write:
- right_line: ONE sentence starting "Also route here when" that describes this kind of ticket in general terms
  (not this exact ticket).
- wrong_line: ONE short clause saying what kind of ticket does NOT belong in the wrongly chosen subcategory.
  Do not name the destination; it is added for you.
Keep both short and specific, and do not repeat what the current descriptions already say."""
    client = OpenAI(api_key=api_key, base_url=config.OPENROUTER_BASE_URL)
    completion = client.chat.completions.create(
        model=config.OPENROUTER_CLASSIFY_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": _schema()},
        temperature=0,
    )
    raw = json.loads(completion.choices[0].message.content)
    return _normalise(raw.get("right_line", ""), raw.get("wrong_line", ""), right)


def apply_lines(data: dict, right_leaf_id: str, wrong_leaf_id: str, right_line: str, wrong_line: str) -> dict:
    """Appends each line (space-separated) to its subcategory's description in
    `data`, in place, skipping a line the description already contains.
    Returns {sub_id: (before, after)}."""
    wanted = {right_leaf_id: right_line.strip(), wrong_leaf_id: wrong_line.strip()}
    out: dict[str, tuple[str, str]] = {}
    for group in data["categories"]:
        for sub in group.get("subcategories", []):
            line = wanted.get(sub["id"])
            if line is None:
                continue
            before = sub.get("description", "") or ""
            after = before
            if line and line not in before:
                after = f"{before} {line}".strip()
            sub["description"] = after
            out[sub["id"]] = (before, after)
    return out
