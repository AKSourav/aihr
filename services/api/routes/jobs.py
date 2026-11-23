from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.job_drafter import draft_job
from core.models import Account, AuditEvent, Job, Organization, Secret
from core.permissions import Permission
from core.providers import merge_selection
from core.schemas import ProviderSelection, QuestionPlan, Rubric
from core.settings import get_settings
from services.api.authz import TenantContext, require
from services.api.routes.provider_config import check_secrets

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


class JobSecretOut(BaseModel):
    secret_id: UUID
    name: str
    last4: str


class JobProvidersOut(BaseModel):
    job_id: UUID
    selection: ProviderSelection
    inherited: ProviderSelection
    secrets: list[JobSecretOut]


class JobProvidersRequest(BaseModel):
    selection: ProviderSelection = ProviderSelection()


async def _job_providers(
    session: AsyncSession, context: TenantContext, job: Job
) -> JobProvidersOut:
    organization = await session.get(Organization, context.org_id)
    account_id = organization.account_id if organization else None
    account = await session.get(Account, account_id) if account_id else None

    rows = (
        (
            await session.scalars(
                select(Secret).where(Secret.account_id == account_id).order_by(Secret.name)
            )
        ).all()
        if account_id
        else []
    )

    return JobProvidersOut(
        job_id=job.id,
        selection=ProviderSelection.model_validate(job.provider_config or {}),
        inherited=merge_selection(
            organization.provider_config if organization else None,
            account.provider_config if account else None,
        ),
        secrets=[
            JobSecretOut(secret_id=row.id, name=row.name, last4=row.last4) for row in rows
        ],
    )


@router.get("/jobs/{job_id}/provider-settings", response_model=JobProvidersOut)
async def read_job_providers(
    job_id: UUID,
    context: TenantContext = Depends(require(Permission.JOB_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> JobProvidersOut:
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == context.org_id))
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return await _job_providers(session, context, job)


@router.put("/jobs/{job_id}/provider-settings", response_model=JobProvidersOut)
async def write_job_providers(
    job_id: UUID,
    body: JobProvidersRequest,
    context: TenantContext = Depends(require(Permission.JOB_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> JobProvidersOut:
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == context.org_id))
    if job is None:
        raise HTTPException(status_code=404, detail="not found")

    organization = await session.get(Organization, context.org_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="not found")
    await check_secrets(session, organization.account_id, body.selection)

    job.provider_config = body.selection.model_dump(mode="json", exclude_none=True)
    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="job",
            entity_id=job.id,
            event_type="provider_settings_updated",
            payload={"scope": "job"},
        )
    )
    await session.commit()

    return await _job_providers(session, context, job)
