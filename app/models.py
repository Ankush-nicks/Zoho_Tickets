from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class TaxonomyUnlockRequest(BaseModel):
    password: str


class ClassificationResult(BaseModel):
    """What the OpenAI structured-output call must return."""
    category_id: str
    confidence: float
    reasoning: str
    needs_clarification: bool
    clarifying_question: str | None = None
    # Not part of the model's response schema - filled in by
    # classifier.apply_reporter_prior when the instructor picked a
    # subcategory, for the routing log: the model's own pick and confidence
    # before the instructor's pick was weighed, and what was decided -
    # 'agreed' (same pick), 'kept' (instructor's pick kept) or 'overrode'.
    # All None when no subcategory was picked (see main._decision_fields).
    model_category_id: str | None = None
    model_confidence: float | None = None
    decision: str | None = None
