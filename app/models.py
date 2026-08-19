from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class NewTicketRequest(BaseModel):
    text: str


class ClarificationResponse(BaseModel):
    answer: str


class CorrectionRequest(BaseModel):
    corrected_category_id: str
    corrected_by: str | None = None


class TicketStateResponse(BaseModel):
    ticket_id: str
    status: str                     # awaiting_clarification | classified | needs_human_review | corrected
    category_id: str | None = None
    category_name: str | None = None
    category_group_id: str | None = None
    category_group_name: str | None = None
    assigned_team: str | None = None
    poc_primary: str | None = None
    poc_cc: str | None = None
    confidence: float | None = None
    reasoning: str | None = None
    clarifying_question: str | None = None
    conversation: list[dict] = []
    # Only set when this ticket originated from a Zoho Creator lookup rather
    # than manual input - None for every existing/manual ticket.
    zoho_ticket_id: str | None = None
    issue_in_detail: str | None = None


class ClassificationResult(BaseModel):
    """What the OpenAI structured-output call must return."""
    category_id: str
    confidence: float
    reasoning: str
    needs_clarification: bool
    clarifying_question: str | None = None
