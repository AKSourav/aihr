from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.job_drafter import draft_job
from core.models import Job
from core.permissions import Permission
from core.schemas import QuestionPlan, Rubric
from core.settings import get_settings
from services.api.authz import TenantContext, require

router = APIRouter(prefix="/tenants/{org_id}", tags=["jobs"])


class JobRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    rubric: Rubric
    question_plan: QuestionPlan


class JobOut(BaseModel):
    job_id: UUID
    title: str
    created_at: datetime


class JobDetailOut(JobOut):
    rubric: list
    question_plan: dict


class JobDraftRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20000)


class JobDraftOut(BaseModel):
    title: str
    rubric: Rubric
    question_plan: QuestionPlan


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(
    body: JobRequest,
    context: TenantContext = Depends(require(Permission.JOB_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    job = Job(
        org_id=context.org_id,
        title=body.title,
        rubric=body.rubric.model_dump(mode="json"),
        question_plan=body.question_plan.model_dump(mode="json"),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    return JobOut(job_id=job.id, title=job.title, created_at=job.created_at)


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    context: TenantContext = Depends(require(Permission.JOB_READ)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobOut]:
    jobs = (
        await session.scalars(
            select(Job)
            .where(Job.org_id == context.org_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return [JobOut(job_id=job.id, title=job.title, created_at=job.created_at) for job in jobs]


@router.post("/jobs/drafts", response_model=JobDraftOut)
async def draft_a_job(
    body: JobDraftRequest,
    context: TenantContext = Depends(require(Permission.JOB_WRITE)),
) -> JobDraftOut:
    settings = get_settings()
    if not settings.llm_api_key or not settings.llm_summary_model:
        raise HTTPException(status_code=503, detail="AI drafting is not configured on this server")

    try:
        draft = await draft_job(body.title, body.description)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="job drafting failed") from exc

    return JobDraftOut(
        title=draft.title, rubric=draft.rubric, question_plan=draft.question_plan
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
async def get_job(
    job_id: UUID,
    context: TenantContext = Depends(require(Permission.JOB_READ)),
    session: AsyncSession = Depends(get_session),
) -> JobDetailOut:
    job = await session.scalar(
        select(Job).where(Job.id == job_id, Job.org_id == context.org_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="not found")

    return JobDetailOut(
        job_id=job.id,
        title=job.title,
        created_at=job.created_at,
        rubric=job.rubric,
        question_plan=job.question_plan,
    )
