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
    # Unix epoch seconds this ticket was created (backdated to the real Zoho
    # "Added Time" for CSV-imported history) - the only reliable per-ticket
    # timestamp, since raw_payload's own added_time is only ever present for
    # CSV-imported tickets, not ones that arrived via the live webhook.
    created_at: float
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
    # Resolution quality grading (app/quality_scorer.py) - None until the
    # ticket is closed AND graded. resolution_score is the avg of the 4
    # criteria fields.
    resolution_score: float | None = None
    resolution_accuracy: float | None = None
    resolution_completeness: float | None = None
    resolution_tone: float | None = None
    resolution_timeliness: float | None = None
    resolution_evidence: str | None = None
    resolution_scored_at: float | None = None
    # Every field Zoho's webhook sent for this ticket (priority, assigned
    # team, POC/worklog history, session ids, etc.) - None for manual/local
    # tickets that never came from a Zoho payload. Shown in full in the UI's
    # "Ticket Details" section so nothing Zoho sent is hidden from a reviewer.
    raw_payload: dict | None = None


class ResolutionGrade(BaseModel):
    """What the QA-grading structured-output call must return - see app/quality_scorer.py."""
    accuracy: float
    completeness: float
    tone: float
    timeliness: float
    evidence: str


class ClassificationResult(BaseModel):
    """What the OpenAI structured-output call must return."""
    category_id: str
    confidence: float
    reasoning: str
    needs_clarification: bool
    clarifying_question: str | None = None
