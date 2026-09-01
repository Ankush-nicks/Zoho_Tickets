import json
from openai import OpenAI, RateLimitError

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
   clarifying question just because the ticket is short - only when it's genuinely ambiguous
   between two or more plausible categories.
3. If the ticket has no discernible topic at all (a greeting, a single word, random
   characters, "test", etc.) such that there's nothing to even ask a clarifying question
   about, route it to the "Other / Unclear" category's id (see taxonomy above) with
   needs_clarification=false rather than guessing a specific category or asking a question.
4. confidence should reflect your true certainty, not be inflated. Use the full 0-1 range.
5. reasoning should be concise (1-2 sentences), referencing what in the text drove the decision.
"""


def classify(ticket_text: str, api_key: str) -> ClassificationResult:
    """
    Dynamic classification: retrieves the most similar known-good examples
    (seed + corrected) and injects them as few-shot context, then asks the
    model for a structured decision.

    Falls back to Cloudflare Workers AI (see _classify_via_cloudflare) on an
    OpenAI RateLimitError, when CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN
    are configured - a completely separate quota from OpenAI's, so a
    classification still goes through instead of stalling until OpenAI's
    own limit resets. Re-raises as before when Cloudflare isn't set up.
    """
    memory.seed_if_empty(taxonomy.seed_examples(), api_key)
    fewshot = memory.retrieve_similar(ticket_text, api_key, k=config.FEWSHOT_K)

    client = OpenAI(api_key=api_key, base_url=config.openai_base_url())
    try:
        completion = client.chat.completions.create(
            model=config.CLASSIFY_MODEL,
            messages=[
                {"role": "system", "content": _build_system_prompt(fewshot)},
                {"role": "user", "content": f"Ticket:\n{ticket_text}"},
            ],
            response_format={"type": "json_schema", "json_schema": _response_schema()},
            temperature=0,
        )
    except RateLimitError:
        if not (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN):
            raise
        return _classify_via_cloudflare(ticket_text, fewshot)
    raw = json.loads(completion.choices[0].message.content)
    return ClassificationResult(**raw)


def _cloudflare_response_schema() -> dict:
    """Same shape as _response_schema(), in the plain-JSON-Schema dialect
    Workers AI's response_format accepts (union-typed nullable field works
    here, unlike Gemini's dialect - no translation needed beyond dropping
    the OpenAI-specific "strict"/"name" wrapper)."""
    return {
        "type": "object",
        "properties": {
            "category_id": {"type": "string", "enum": taxonomy.category_ids},
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"},
            "needs_clarification": {"type": "boolean"},
            "clarifying_question": {"type": ["string", "null"]},
        },
        "required": ["category_id", "confidence", "reasoning", "needs_clarification", "clarifying_question"],
    }


def _classify_via_cloudflare(ticket_text: str, fewshot: list[dict]) -> ClassificationResult:
    import httpx

    url = f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/run/{config.CLOUDFLARE_WORKERS_AI_MODEL}"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"},
        json={
            "messages": [
                {"role": "system", "content": _build_system_prompt(fewshot)},
                {"role": "user", "content": f"Ticket:\n{ticket_text}"},
            ],
            "response_format": {"type": "json_schema", "json_schema": _cloudflare_response_schema()},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Workers AI error: {data.get('errors')}")
    return ClassificationResult(**data["result"]["response"])


def _gemini_response_schema() -> dict:
    """
    Same shape/constraints as _response_schema(), translated to Gemini's
    schema dialect (a subset of OpenAPI 3.0 - uppercase type names, no
    additionalProperties, nullable instead of a ["string","null"] union).
    """
    return {
        "type": "OBJECT",
        "properties": {
            "category_id": {
                "type": "STRING",
                "enum": taxonomy.category_ids,
                "description": "Best-matching taxonomy category id.",
            },
            "confidence": {
                "type": "NUMBER",
                "description": "0.0-1.0 confidence that category_id is correct given ONLY the information provided.",
            },
            "reasoning": {
                "type": "STRING",
                "description": "One or two sentences on why this category was chosen.",
            },
            "needs_clarification": {
                "type": "BOOLEAN",
                "description": "True if the ticket text is too vague/ambiguous to confidently route.",
            },
            "clarifying_question": {
                "type": "STRING",
                "nullable": True,
                "description": "A single, specific question to ask the user if needs_clarification is true, else null.",
            },
        },
        "required": ["category_id", "confidence", "reasoning", "needs_clarification", "clarifying_question"],
    }


def classify_gemini(ticket_text: str, gemini_api_key: str, embed_api_key: str) -> ClassificationResult:
    """
    Same taxonomy/prompt/few-shot pipeline as classify(), but the actual
    classification call goes to Gemini instead of OpenAI - for side-by-side
    accuracy comparison (see scripts/compare_classifiers.py). Few-shot
    retrieval still uses OpenAI embeddings (embed_api_key) since that's the
    only embedding backend this app has - only the classification model
    itself is being compared, not the retrieval step.
    """
    from google import genai
    from google.genai import types

    memory.seed_if_empty(taxonomy.seed_examples(), embed_api_key)
    fewshot = memory.retrieve_similar(ticket_text, embed_api_key, k=config.FEWSHOT_K)

    client = genai.Client(api_key=gemini_api_key)
    response = client.models.generate_content(
        model=config.GEMINI_CLASSIFY_MODEL,
        contents=f"Ticket:\n{ticket_text}",
        config=types.GenerateContentConfig(
            system_instruction=_build_system_prompt(fewshot),
            response_mime_type="application/json",
            response_schema=_gemini_response_schema(),
            temperature=0,
        ),
    )
    raw = json.loads(response.text)
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
