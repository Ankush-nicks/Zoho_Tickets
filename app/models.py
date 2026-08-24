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
    # Whatever category/sub-category Zoho already had on the record (e.g. set
    # by whoever raised the ticket), kept purely for comparison against our
    # own prediction - None when the source ticket didn't carry one.
    zoho_category: str | None = None
    zoho_subcategory: str | None = None
    # None when there's nothing to compare (no zoho_subcategory); otherwise
    # whether our predicted category loosely matches Zoho's own tag.
    zoho_agrees: bool | None = None


class ClassificationResult(BaseModel):
    """What the OpenAI structured-output call must return."""
    category_id: str
    confidence: float
    reasoning: str
    needs_clarification: bool
    clarifying_question: str | None = None
