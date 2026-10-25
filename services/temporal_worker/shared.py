from dataclasses import dataclass


@dataclass
class ScreeningCallInput:
    application_id: str
    call_mode: str = "web"
    skip_business_hours: bool = False
    fast_retries: bool = False


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
