import json
from dataclasses import dataclass

from openai import OpenAI, RateLimitError

from . import config
from .taxonomy import taxonomy
from . import memory
from .models import ClassificationResult


@dataclass(frozen=True)
class ReporterHint:
    """
    What the instructor picked on the Zoho form (Category_Of_The_Issue /
    the optional Sub_Category_Of_The_Issue) before our classifier ran,
    already resolved to taxonomy ids (see main.py's _reporter_hint_from).
    Zoho's form uses this same taxonomy, and the pick often carries intent
    the free text leaves out - a bare "not working" filed under Recording
    Issue is a recording issue - so it's fed to the model as a strong prior
    and backed up by apply_reporter_prior() rather than silently overwritten.

    leaf_id is None when only a category group was picked; group_id is
    always set (the leaf's parent when a subcategory was picked).
    """
    group_id: str
    leaf_id: str | None = None

    def candidate_leaf_ids(self) -> list[str]:
        """The reporter's leaf plus its siblings - the extra few-shot pool."""
        return [
            leaf_id for leaf_id in taxonomy.category_ids
            if taxonomy.get(leaf_id)["parent_id"] == self.group_id
        ]


def _build_user_message(ticket_text: str, hint: ReporterHint | None) -> str:
    msg = f"Ticket:\n{ticket_text}"
    if hint is None:
        return msg
    leaf = taxonomy.get(hint.leaf_id) if hint.leaf_id else None
    if leaf:
        picked = f"subcategory {hint.leaf_id} ({leaf['parent_name']} > {leaf['name']})"
    else:
        group = next((g for g in taxonomy.groups if g["id"] == hint.group_id), None)
        group_name = group["name"] if group else hint.group_id
        picked = f"category group {hint.group_id} ({group_name}) - no subcategory picked"
    return f"{msg}\n\nREPORTER-SELECTED CATEGORY (chosen by the instructor who raised this ticket): {picked}"


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
6. If the ticket includes a REPORTER-SELECTED CATEGORY line, the instructor who raised it
   picked that from this same taxonomy, and it often carries intent the free text leaves out.
   Treat it as a strong prior: keep it unless the ticket text clearly and specifically
   describes a different subcategory's issue. If only a category group was picked, prefer a
   subcategory inside that group. If you do pick something else, say in reasoning what in
   the text contradicts the reporter's choice. Agreeing with the reporter's pick is never
   by itself a reason to ask a clarifying question.
"""


def classify(
    ticket_text: str,
    api_key: str,
    embed_api_key: str | None = None,
    reporter_hint: ReporterHint | None = None,
) -> ClassificationResult:
    """
    Dynamic classification: retrieves the most similar known-good examples
    (seed + corrected) and injects them as few-shot context, then asks the
    model for a structured decision.

    api_key is an OpenRouter key for the actual classification call.
    embed_api_key is a separate OpenAI key for few-shot memory embeddings
    (OpenRouter has no embeddings endpoint) - defaults to config.OPENAI_
    API_KEY when omitted, since that's a fixed server-side value, not
    something callers normally need to pass explicitly.

    Falls back to Cloudflare Workers AI (see _classify_via_cloudflare) on a
    RateLimitError, when CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN are
    configured - a completely separate quota from OpenRouter's, so a
    classification still goes through instead of stalling until OpenRouter's
    own limit resets. Re-raises as before when Cloudflare isn't set up.

    reporter_hint (what the instructor picked on the Zoho form, if anything)
    is shown to the model, widens few-shot retrieval to the reporter's
    category, and then gates the final answer via apply_reporter_prior().
    """
    embed_api_key = embed_api_key or config.OPENAI_API_KEY
    fewshot = _retrieve_fewshot(ticket_text, embed_api_key, reporter_hint)
    user_message = _build_user_message(ticket_text, reporter_hint)

    client = OpenAI(api_key=api_key, base_url=config.OPENROUTER_BASE_URL)
    try:
        completion = client.chat.completions.create(
            model=config.OPENROUTER_CLASSIFY_MODEL,
            messages=[
                {"role": "system", "content": _build_system_prompt(fewshot)},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_schema", "json_schema": _response_schema()},
            temperature=0,
        )
    except RateLimitError:
        if not (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN):
            raise
        return apply_reporter_prior(_classify_via_cloudflare(user_message, fewshot), reporter_hint)
    raw = json.loads(completion.choices[0].message.content)
    return apply_reporter_prior(ClassificationResult(**raw), reporter_hint)


def _retrieve_fewshot(ticket_text: str, embed_api_key: str, hint: ReporterHint | None) -> list[dict]:
    memory.seed_if_empty(taxonomy.seed_examples(), embed_api_key)
    return memory.retrieve_similar(
        ticket_text, embed_api_key, k=config.FEWSHOT_K,
        also_from_categories=hint.candidate_leaf_ids() if hint else None,
    )


def apply_reporter_prior(result: ClassificationResult, hint: ReporterHint | None) -> ClassificationResult:
    """
    Code-level backstop for prompt rule 6. Only applies when the instructor
    picked a specific subcategory - a group-only pick is left to the prompt,
    since there's no single leaf to fall back to:

    - Model agrees with the reporter: two independent signals agree, so
      don't ask a clarifying question, and lift confidence to at least
      CONFIDENCE_THRESHOLD so it routes instead of going to human review.
    - Model disagrees but isn't at least REPORTER_OVERRIDE_MIN_CONFIDENCE
      sure (or wanted to clarify): keep the reporter's pick. Overwriting it
      on a weak hunch is exactly what was misrouting tickets.
    - Model disagrees confidently: keep the model's pick - the reporter can
      be wrong too, that's the point of classifying at all.
    """
    if hint is None or not hint.leaf_id or taxonomy.get(hint.leaf_id) is None:
        return result
    if result.category_id == hint.leaf_id:
        return result.model_copy(update={
            "confidence": max(result.confidence, config.CONFIDENCE_THRESHOLD),
            "needs_clarification": False,
            "clarifying_question": None,
        })
    if not result.needs_clarification and result.confidence >= config.REPORTER_OVERRIDE_MIN_CONFIDENCE:
        return result.model_copy(update={
            "reasoning": f"Overrode the instructor's pick ({hint.leaf_id}). {result.reasoning}",
        })
    return ClassificationResult(
        category_id=hint.leaf_id,
        confidence=config.CONFIDENCE_THRESHOLD,
        reasoning=(
            f"Kept the instructor's pick ({hint.leaf_id}); the model leaned toward "
            f"{result.category_id} ({result.confidence:.2f}) but not confidently enough to override. "
            f"Model reasoning: {result.reasoning}"
        ),
        needs_clarification=False,
        clarifying_question=None,
    )


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


def _extract_cloudflare_json(data: dict) -> dict:
    """
    Workers AI's structured output isn't 100% guaranteed to land as a
    parsed object - on some inputs result.response comes back as a raw
    JSON string that still needs a json.loads, or is missing entirely
    with the JSON sitting in the plain chat message content instead.
    """
    result = data.get("result") or {}
    response = result.get("response")
    if isinstance(response, dict):
        return response
    if isinstance(response, str) and response.strip():
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
    content = (((result.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if content:
        return json.loads(content)
    raise RuntimeError(f"Cloudflare Workers AI returned no parseable structured output: {data}")


def _classify_via_cloudflare(user_message: str, fewshot: list[dict]) -> ClassificationResult:
    import httpx

    url = f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/run/{config.CLOUDFLARE_WORKERS_AI_MODEL}"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"},
        json={
            "messages": [
                {"role": "system", "content": _build_system_prompt(fewshot)},
                {"role": "user", "content": user_message},
            ],
            "response_format": {"type": "json_schema", "json_schema": _cloudflare_response_schema()},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Workers AI error: {data.get('errors')}")
    return ClassificationResult(**_extract_cloudflare_json(data))


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


def classify_gemini(
    ticket_text: str,
    gemini_api_key: str,
    embed_api_key: str,
    reporter_hint: ReporterHint | None = None,
) -> ClassificationResult:
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

    fewshot = _retrieve_fewshot(ticket_text, embed_api_key, reporter_hint)

    client = genai.Client(api_key=gemini_api_key)
    response = client.models.generate_content(
        model=config.GEMINI_CLASSIFY_MODEL,
        contents=_build_user_message(ticket_text, reporter_hint),
        config=types.GenerateContentConfig(
            system_instruction=_build_system_prompt(fewshot),
            response_mime_type="application/json",
            response_schema=_gemini_response_schema(),
            temperature=0,
        ),
    )
    raw = json.loads(response.text)
    return apply_reporter_prior(ClassificationResult(**raw), reporter_hint)


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
