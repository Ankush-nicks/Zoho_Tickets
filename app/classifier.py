import json
from openai import OpenAI

from . import config
from .taxonomy import taxonomy
from . import memory
from .models import ClassificationResult


def _response_schema() -> dict:
    """JSON schema constrained to the *current* taxonomy's category ids."""
    return {
        "name": "ticket_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "category_id": {
                    "type": "string",
                    "enum": taxonomy.category_ids,
                    "description": "Best-matching taxonomy category id.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0 confidence that category_id is correct given ONLY the information provided.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this category was chosen.",
                },
                "needs_clarification": {
                    "type": "boolean",
                    "description": "True if the ticket text is too vague/ambiguous to confidently route.",
                },
                "clarifying_question": {
                    "type": ["string", "null"],
                    "description": "A single, specific question to ask the user if needs_clarification is true, else null.",
                },
            },
            "required": ["category_id", "confidence", "reasoning", "needs_clarification", "clarifying_question"],
            "additionalProperties": False,
        },
    }


def _build_system_prompt(fewshot: list[dict]) -> str:
    fewshot_block = "\n".join(
        f'- "{ex["text"]}" -> {ex["category_id"]}' for ex in fewshot
    ) or "(no prior examples yet)"

    return f"""You are a support-ticket intent classifier. Tickets are routed by
subcategory, and each subcategory maps to a specific team and point of contact,
so precision matters more than picking "close enough."

TAXONOMY (choose exactly one category_id - a subcategory id - from this list):
{taxonomy.as_prompt_block()}

SIMILAR PAST EXAMPLES (retrieved because they resemble this ticket; some come from
corrected human labels and should be weighted heavily as ground truth):
{fewshot_block}

RULES:
1. Pick the single best-matching category_id from the taxonomy above. Never invent an id.
2. If the ticket text genuinely does not give you enough information to confidently
   distinguish between two or more categories, set needs_clarification=true and write ONE
   specific, short clarifying question that would resolve the ambiguity. Do not ask a
   clarifying question just because the ticket is short - only when it's genuinely ambiguous.
3. confidence should reflect your true certainty, not be inflated. Use the full 0-1 range.
4. reasoning should be concise (1-2 sentences), referencing what in the text drove the decision.
"""


def classify(ticket_text: str, api_key: str) -> ClassificationResult:
    """
    Dynamic classification: retrieves the most similar known-good examples
    (seed + corrected) and injects them as few-shot context, then asks the
    model for a structured decision.
    """
    memory.seed_if_empty(taxonomy.seed_examples(), api_key)
    fewshot = memory.retrieve_similar(ticket_text, api_key, k=config.FEWSHOT_K)

    client = OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model=config.CLASSIFY_MODEL,
        messages=[
            {"role": "system", "content": _build_system_prompt(fewshot)},
            {"role": "user", "content": f"Ticket:\n{ticket_text}"},
        ],
        response_format={"type": "json_schema", "json_schema": _response_schema()},
        temperature=0,
    )
    raw = json.loads(completion.choices[0].message.content)
    return ClassificationResult(**raw)


def should_finalize(result: ClassificationResult, clarification_turns: int) -> bool:
    """
    Decide whether to accept the classification or ask another clarifying
    question. Stops asking after MAX_CLARIFICATION_TURNS regardless of
    confidence, and routes to human review instead (see main.py).
    """
    if clarification_turns >= config.MAX_CLARIFICATION_TURNS:
        return True
    if result.needs_clarification:
        return False
    if result.confidence < config.CONFIDENCE_THRESHOLD:
        return False
    return True
