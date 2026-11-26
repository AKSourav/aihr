import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, RootModel, model_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_http(value: str | None) -> bool:
    return bool(value) and str(value).lower().startswith(("http://", "https://"))


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


class SlotConfig(BaseModel):
    provider: str | None = None
    secret_id: UUID | None = None
    model: str | None = None
    voice_id: str | None = None
    base_url: str | None = None


class ProviderSelection(BaseModel):
    stt: SlotConfig = SlotConfig()
    tts: SlotConfig = SlotConfig()
    llm_call: SlotConfig = SlotConfig()
    llm_summary: SlotConfig = SlotConfig()


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


class ResumeScore(BaseModel):
    rubric_scores: list[RubricScore]
    confidence: Literal["low", "medium", "high"]


class ShortlistRules(BaseModel):
    top_k: int | None = Field(default=None, ge=1, le=1000)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    combine: Literal["all", "any"] = "all"
    auto_advance: bool = True


class CohortNote(BaseModel):
    name: str = Field(max_length=200)
    strengths: list[str] = Field(default=[], max_length=10)
    gaps: list[str] = Field(default=[], max_length=10)


class CohortSummary(BaseModel):
    summary: str = Field(max_length=4000)
    candidates: list[CohortNote] = Field(default=[], max_length=10)
    recommendation: str = Field(max_length=2000)


class ResumeLinks(BaseModel):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    website: str | None = None
    other: list[str] = Field(default=[], max_length=5)

    @model_validator(mode="after")
    def drop_non_http_links(self) -> "ResumeLinks":
        for field in ("linkedin", "github", "portfolio", "website"):
            if not _is_http(getattr(self, field)):
                setattr(self, field, None)
        self.other = [link for link in self.other if _is_http(link)]
        return self


class ResumeExperience(BaseModel):
    company: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    start: str | None = Field(default=None, max_length=32)
    end: str | None = Field(default=None, max_length=32)
    summary: str = Field(default="", max_length=600)


class ResumeEducation(BaseModel):
    institution: str = Field(default="", max_length=200)
    degree: str | None = Field(default=None, max_length=200)
    year: str | None = Field(default=None, max_length=32)


class ResumeExtract(BaseModel):
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=320)
    phone_e164: str | None = Field(default=None, max_length=20)
    links: ResumeLinks = ResumeLinks()
    skills: list[str] = Field(default=[], max_length=50)
    experience: list[ResumeExperience] = Field(default=[], max_length=20)
    education: list[ResumeEducation] = Field(default=[], max_length=10)
    total_years_experience: float | None = Field(default=None, ge=0, le=70)
