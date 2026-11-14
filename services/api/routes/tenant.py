import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application, Call
from core.models import CallSummary as CallSummaryRow
from core.models import Score
from core.permissions import Permission
from core.settings import get_settings
from core.storage import Storage
from services.api.authz import TenantContext, require

router = APIRouter(prefix="/tenants/{org_id}", tags=["tenant"])


class ScoreOut(BaseModel):
    stage: str
    breakdown: dict
    created_at: datetime


class CallOut(BaseModel):
    call_id: UUID
    attempt_no: int
    call_mode: str
    status: str
    duration_seconds: int | None
    created_at: datetime


class ApplicationOut(BaseModel):
    application_id: UUID
    candidate_name: str
    candidate_email: str
    job_title: str
    stage: str
    overall_score: float | None
    created_at: datetime


class ApplicationDetailOut(ApplicationOut):
    scores: list[ScoreOut]
    calls: list[CallOut]


class CallSummaryOut(BaseModel):
    call_id: UUID
    summary: str
    rubric_scores: list
    facts: dict
    red_flags: list
    candidate_questions: list
    confidence: str
    model_version: str
    prompt_version: str


class TranscriptOut(BaseModel):
    call_id: UUID
    turns: list[dict[str, Any]]


def _overall(score: Score | None) -> float | None:
    if score is None:
        return None
    value = score.breakdown.get("overall")
    return float(value) if isinstance(value, (int, float)) else None


def _as_application(application: Application, score: Score | None) -> ApplicationOut:
    candidate = application.candidate
    return ApplicationOut(
        application_id=application.id,
        candidate_name=f"{candidate.first_name} {candidate.last_name}",
        candidate_email=candidate.email,
        job_title=application.job.title,
        stage=application.stage,
        overall_score=_overall(score),
        created_at=application.created_at,
    )


@router.get("/applications", response_model=list[ApplicationOut])
async def list_applications(
    context: TenantContext = Depends(require(Permission.SCORE_READ)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ApplicationOut]:
    applications = (
        await session.scalars(
            select(Application)
            .where(Application.org_id == context.org_id)
            .order_by(Application.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    if not applications:
        return []

    scores = (
        await session.scalars(
            select(Score)
            .where(
                Score.org_id == context.org_id,
                Score.application_id.in_([a.id for a in applications]),
            )
            .order_by(Score.created_at.desc())
        )
    ).all()

    latest: dict[UUID, Score] = {}
    for score in scores:
        latest.setdefault(score.application_id, score)

    return [_as_application(a, latest.get(a.id)) for a in applications]


@router.get("/applications/{application_id}", response_model=ApplicationDetailOut)
async def get_application(
    application_id: UUID,
    context: TenantContext = Depends(require(Permission.SCORE_READ)),
    session: AsyncSession = Depends(get_session),
) -> ApplicationDetailOut:
    application = await session.scalar(
        select(Application).where(
            Application.id == application_id, Application.org_id == context.org_id
        )
    )
    if application is None:
        raise HTTPException(status_code=404, detail="not found")

    scores = (
        await session.scalars(
            select(Score)
            .where(Score.org_id == context.org_id, Score.application_id == application_id)
            .order_by(Score.created_at.desc())
        )
    ).all()
    calls = (
        await session.scalars(
            select(Call)
            .where(Call.org_id == context.org_id, Call.application_id == application_id)
            .order_by(Call.created_at.desc())
        )
    ).all()

    base = _as_application(application, scores[0] if scores else None)
    return ApplicationDetailOut(
        **base.model_dump(),
        scores=[
            ScoreOut(stage=s.stage, breakdown=s.breakdown, created_at=s.created_at) for s in scores
        ],
        calls=[
            CallOut(
                call_id=c.id,
                attempt_no=c.attempt_no,
                call_mode=c.call_mode,
                status=c.status,
                duration_seconds=c.duration_seconds,
                created_at=c.created_at,
            )
            for c in calls
        ],
    )


@router.get("/calls/{call_id}", response_model=CallOut)
async def get_call(
    call_id: UUID,
    context: TenantContext = Depends(require(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
) -> CallOut:
    call = await session.scalar(
        select(Call).where(Call.id == call_id, Call.org_id == context.org_id)
    )
    if call is None:
        raise HTTPException(status_code=404, detail="not found")

    return CallOut(
        call_id=call.id,
        attempt_no=call.attempt_no,
        call_mode=call.call_mode,
        status=call.status,
        duration_seconds=call.duration_seconds,
        created_at=call.created_at,
    )


@router.get("/calls/{call_id}/summary", response_model=CallSummaryOut)
async def get_call_summary(
    call_id: UUID,
    context: TenantContext = Depends(require(Permission.SCORE_READ)),
    session: AsyncSession = Depends(get_session),
) -> CallSummaryOut:
    row = await session.scalar(
        select(CallSummaryRow).where(
            CallSummaryRow.call_id == call_id, CallSummaryRow.org_id == context.org_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    return CallSummaryOut(
        call_id=row.call_id,
        summary=row.summary,
        rubric_scores=row.rubric_scores,
        facts=row.facts,
        red_flags=row.red_flags,
        candidate_questions=row.candidate_questions,
        confidence=row.confidence,
        model_version=row.model_version,
        prompt_version=row.prompt_version,
    )


@router.get("/calls/{call_id}/transcript", response_model=TranscriptOut)
async def get_call_transcript(
    call_id: UUID,
    context: TenantContext = Depends(require(Permission.TRANSCRIPT_READ)),
    session: AsyncSession = Depends(get_session),
) -> TranscriptOut:
    call = await session.scalar(
        select(Call).where(Call.id == call_id, Call.org_id == context.org_id)
    )
    if call is None or call.transcript_url is None:
        raise HTTPException(status_code=404, detail="not found")

    key = call.transcript_url.removeprefix(f"s3://{get_settings().s3_bucket}/")
    turns = await asyncio.to_thread(Storage().download_json, key)

    return TranscriptOut(call_id=call.id, turns=turns)
