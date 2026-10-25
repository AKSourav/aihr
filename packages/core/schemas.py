from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class CallMode(str, Enum):
    console = "console"
    web = "web"
    sim = "sim"
    sip = "sip"


class FaqItem(BaseModel):
    q: str
    a: str


class QuestionItem(BaseModel):
    id: str
    text: str
    probe: str | None = None
    required: bool = False
    extract: str | list[str] | None = None


class QuestionPlan(BaseModel):
    intro_pitch: str
    questions: list[QuestionItem]
    faq: list[FaqItem] = []
    max_minutes: int


class CallContext(BaseModel):
    call_id: UUID
    application_id: UUID
    call_mode: CallMode
    phone_e164: str | None
    company_name: str
    job_title: str
    candidate_first_name: str
    profile_summary: str
    question_plan: QuestionPlan
    recording_consent_on_file: bool
    max_minutes: int


class Turn(BaseModel):
    speaker: Literal["agent", "candidate"]
    text: str
    started_at: datetime


class CallStatus(str, Enum):
    completed = "completed"
    callback_requested = "callback_requested"
    voicemail = "voicemail"
    busy = "busy"
    no_answer = "no_answer"
    declined = "declined"
    failed = "failed"
    lost = "lost"


class CallOutcome(BaseModel):
    call_id: UUID
    status: CallStatus
    transcript: list[Turn] = []
    callback_at: datetime | None = None
    answered_question_ids: list[str] = []
    duration_seconds: int | None = None
    error_detail: str | None = None


class RubricScore(BaseModel):
    dimension: str
    score: float
    evidence: str


class CallSummary(BaseModel):
    summary: str
    rubric_scores: list[RubricScore]
    facts: dict[str, Any]
    red_flags: list[str]
    candidate_questions: list[str]
    confidence: Literal["low", "medium", "high"]
