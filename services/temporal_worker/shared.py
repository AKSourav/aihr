from dataclasses import dataclass


@dataclass
class ScreeningCallInput:
    application_id: str
    call_mode: str = "web"
    skip_business_hours: bool = False
    fast_retries: bool = False
    signal_timeout_min: int = 45
    invite_ttl_hours: int = 72


@dataclass
class SendInviteInput:
    call_id: str
    application_id: str
    attempt_no: int
    ttl_hours: int


@dataclass
class ExpireInviteInput:
    call_id: str


@dataclass
class PlaceCallInput:
    call_id: str
    application_id: str
    attempt_no: int
    call_mode: str


@dataclass
class PlaceCallResult:
    call_id: str
    room_name: str


@dataclass
class MarkUnreachableInput:
    application_id: str


@dataclass
class SummarizeCallInput:
    call_id: str


@dataclass
class ExtractResumeInput:
    resume_id: str


@dataclass
class FailResumeInput:
    resume_id: str
    reason: str


@dataclass
class ScoreResumeInput:
    application_id: str
