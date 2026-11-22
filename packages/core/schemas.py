from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, RootModel, model_validator


class CallMode(str, Enum):
    console = "console"
    web = "web"
    sim = "sim"
    sip = "sip"
    email = "email"


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
    questions: list[QuestionItem] = Field(min_length=1)
    faq: list[FaqItem] = []
    max_minutes: int = Field(ge=1, le=60)

    @model_validator(mode="after")
    def check_unique_question_ids(self) -> "QuestionPlan":
        ids = [question.id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        return self


class RubricDimension(BaseModel):
    dimension: str = Field(min_length=1, max_length=64)
    weight: float = Field(gt=0)
    description: str = Field(min_length=1, max_length=400)


class Rubric(RootModel[list[RubricDimension]]):
    @model_validator(mode="after")
    def check_dimensions(self) -> "Rubric":
        if not self.root:
            raise ValueError("rubric must have at least one dimension")
        dimensions = [item.dimension for item in self.root]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("rubric dimensions must be unique")
        return self


class ProviderSelection(BaseModel):
    stt_provider: str | None = None
    stt_model: str | None = None
    tts_provider: str | None = None
    tts_model: str | None = None
    tts_voice_id: str | None = None
    llm_provider: str | None = None
    llm_call_model: str | None = None
    llm_summary_model: str | None = None
    llm_base_url: str | None = None


class ResolvedProvider(BaseModel):
    provider: str
    model: str = ""
    api_key: str = Field(default="", repr=False)
    base_url: str | None = None
    voice_id: str | None = None


class CallProviders(BaseModel):
    stt: ResolvedProvider
    tts: ResolvedProvider
    llm: ResolvedProvider


class CallContext(BaseModel):
    call_id: UUID
    application_id: UUID
    call_mode: CallMode
    phone_e164: str | None
    company_name: str
    job_title: str
    candidate_first_name: str
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
    score: float = Field(ge=0.0, le=1.0)
    evidence: str


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class CallSummary(BaseModel):
    summary: str
    rubric_scores: list[RubricScore]
    facts: dict[str, Any]
    red_flags: list[str]
    candidate_questions: list[str]
    confidence: Literal["low", "medium", "high"]
