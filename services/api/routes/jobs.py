from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cohort_summarizer import summarize_cohort
from core.db import get_session
from core.job_drafter import draft_job
from core.llm import provider_detail
from core.models import Account, Application, AuditEvent, Job, Organization, Secret
from core.permissions import Permission
from core.providers import ProviderNotConfigured, merge_selection, resolve_org_llm
from core.resumes import READY, current_resume
from core.schemas import CohortNote, ProviderSelection, QuestionPlan, Rubric, ShortlistRules
from core.settings import get_settings
from core.shortlist import apply_rules, qualifies, ranked_select, rules_of
from services.api.authz import TenantContext, require
from services.api.routes.provider_config import check_secrets
from services.api.routes.tenant import ApplicationOut

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
    session: AsyncSession = Depends(get_session),
) -> JobDraftOut:
    organization = await session.get(Organization, context.org_id)
    llm = None
    if organization is not None:
        try:
            llm = await resolve_org_llm(session, organization)
        except ProviderNotConfigured:
            llm = None

    settings = get_settings()
    api_key = llm.api_key if llm else settings.llm_api_key
    model = (llm.model if llm else "") or settings.llm_summary_model
    if not api_key or not model:
        raise HTTPException(status_code=503, detail="AI drafting is not configured on this server")

    try:
        draft = await draft_job(body.title, body.description, llm)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=provider_detail(exc, "job drafting failed")
        ) from exc

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


class JobCandidateOut(ApplicationOut):
    resume_score: float | None
    call_score: float | None
    rank: int
    qualifies: bool


class JobRulesOut(BaseModel):
    job_id: UUID
    rules: ShortlistRules
    advanced: int = 0


class JobRulesRequest(BaseModel):
    rules: ShortlistRules = ShortlistRules()


async def _job_in_org(session: AsyncSession, org_id: UUID, job_id: UUID) -> Job:
    job = await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == org_id))
    if job is None:
        raise HTTPException(status_code=404, detail="not found")
    return job


@router.get("/jobs/{job_id}/candidates", response_model=list[JobCandidateOut])
async def list_job_candidates(
    job_id: UUID,
    context: TenantContext = Depends(require(Permission.SCORE_READ)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobCandidateOut]:
    job = await _job_in_org(session, context.org_id, job_id)
    rules = rules_of(job)

    rows = (
        await session.execute(
            ranked_select(context.org_id, job.id).limit(limit).offset(offset)
        )
    ).all()

    return [
        JobCandidateOut(
            application_id=application.id,
            candidate_id=application.candidate_id,
            candidate_name=f"{application.candidate.first_name} {application.candidate.last_name}",
            candidate_email=application.candidate.email,
            job_id=application.job_id,
            job_title=application.job.title,
            stage=application.stage,
            overall_score=overall,
            created_at=application.created_at,
            resume_score=resume_score,
            call_score=call_score,
            rank=rank,
            qualifies=qualifies(rules, rank, overall),
        )
        for application, resume_score, call_score, overall, rank in rows
    ]


@router.get("/jobs/{job_id}/shortlist-rules", response_model=JobRulesOut)
async def read_shortlist_rules(
    job_id: UUID,
    context: TenantContext = Depends(require(Permission.JOB_READ)),
    session: AsyncSession = Depends(get_session),
) -> JobRulesOut:
    job = await _job_in_org(session, context.org_id, job_id)
    return JobRulesOut(job_id=job.id, rules=rules_of(job))


@router.put("/jobs/{job_id}/shortlist-rules", response_model=JobRulesOut)
async def write_shortlist_rules(
    job_id: UUID,
    body: JobRulesRequest,
    context: TenantContext = Depends(require(Permission.JOB_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> JobRulesOut:
    job = await _job_in_org(session, context.org_id, job_id)

    job.shortlist_rules = body.rules.model_dump(mode="json", exclude_none=True)
    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="job",
            entity_id=job.id,
            event_type="shortlist_rules_updated",
            payload=job.shortlist_rules,
        )
    )
    await session.flush()
    advanced = await apply_rules(session, job, context.user.id)
    await session.commit()

    return JobRulesOut(job_id=job.id, rules=body.rules, advanced=len(advanced))


MAX_COHORT = 10


class CandidateSummaryRequest(BaseModel):
    application_ids: list[UUID] = Field(min_length=2, max_length=MAX_COHORT)


class CandidateSummaryOut(BaseModel):
    summary: str
    candidates: list[CohortNote]
    recommendation: str


@router.post("/jobs/{job_id}/candidate-summary", response_model=CandidateSummaryOut)
async def summarize_candidates(
    job_id: UUID,
    body: CandidateSummaryRequest,
    context: TenantContext = Depends(require(Permission.CANDIDATE_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> CandidateSummaryOut:
    job = await _job_in_org(session, context.org_id, job_id)

    applications = (
        await session.scalars(
            select(Application).where(
                Application.org_id == context.org_id,
                Application.job_id == job.id,
                Application.id.in_(body.application_ids),
            )
        )
    ).all()
    if len(applications) != len(set(body.application_ids)):
        raise HTTPException(
            status_code=404, detail="one or more applications are not on this job"
        )

    cohort: list[tuple[str, str]] = []
    missing: list[str] = []
    for application in applications:
        candidate = application.candidate
        name = f"{candidate.first_name} {candidate.last_name}".strip()
        resume = await current_resume(session, context.org_id, candidate.id)
        if resume is None or resume.status != READY or not resume.text:
            missing.append(name)
            continue
        cohort.append((name, resume.text))

    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"no extracted resume yet for: {', '.join(sorted(missing))}",
        )

    organization = await session.get(Organization, context.org_id)
    llm = None
    if organization is not None:
        try:
            llm = await resolve_org_llm(session, organization)
        except ProviderNotConfigured:
            llm = None

    settings = get_settings()
    api_key = llm.api_key if llm else settings.llm_api_key
    model = (llm.model if llm else "") or settings.llm_summary_model
    if not api_key or not model:
        raise HTTPException(
            status_code=503, detail="AI summaries are not configured on this server"
        )

    try:
        summary, usage = await summarize_cohort(job.title, job.rubric, cohort, llm)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=provider_detail(exc, "candidate summary failed")
        ) from exc

    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="job",
            entity_id=job.id,
            event_type="candidate_summary_generated",
            payload={
                "application_ids": [str(a.id) for a in applications],
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            },
        )
    )
    await session.commit()

    return CandidateSummaryOut(
        summary=summary.summary,
        candidates=summary.candidates,
        recommendation=summary.recommendation,
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
