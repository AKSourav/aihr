"""The assistant's tools, defined once and served to both the in-app loop and MCP.

Every tool takes the caller's session and org_id. `org_id` is deliberately not a parameter of
any tool's argument model, so the model cannot express a cross-tenant request at all.
"""

import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from openai.types.chat import ChatCompletionToolParam
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    Application,
    AssistantPendingAction,
    AuditEvent,
    Call,
    CallSummary,
    Candidate,
    Job,
    Resume,
    Score,
)
from core.permissions import Permission
from core.resumes import READY
from core.shortlist import CALL_STAGE, RESUME_STAGE, qualifies, ranked_select, rules_of

MAX_PROPOSED = 10
LIVE_CALL_STATUSES = {"dispatching", "ringing", "in_progress", "invited"}

_conversation_id: ContextVar[uuid.UUID] = ContextVar("assistant_conversation_id")


def set_conversation(conversation_id: uuid.UUID):
    return _conversation_id.set(conversation_id)


def reset_conversation(token) -> None:
    _conversation_id.reset(token)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    permission: Permission
    run: Callable[[AsyncSession, UUID, UUID, Any], Awaitable[dict]]


class ListJobsArgs(BaseModel):
    query: str | None = Field(
        default=None, max_length=200, description="case-insensitive substring of the job title"
    )
    limit: int = Field(default=10, ge=1, le=25)


class RankCandidatesArgs(BaseModel):
    job_id: UUID = Field(description="the job to rank candidates for")
    stage: str | None = Field(
        default=None, max_length=50, description="only candidates at this pipeline stage"
    )
    limit: int = Field(default=10, ge=1, le=25)


class SearchCandidatesArgs(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="name or email fragment; a literal substring match, not semantic search",
    )
    job_id: UUID | None = Field(default=None, description="restrict to candidates on this job")
    limit: int = Field(default=10, ge=1, le=25)


class ApplicationArgs(BaseModel):
    application_id: UUID


class DraftQuestionsArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(
        min_length=1, max_length=20000, description="the job description to draft from"
    )


class ProposeScreeningArgs(BaseModel):
    application_ids: list[UUID] = Field(min_length=1, max_length=MAX_PROPOSED)
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="why these candidates, in terms of rubric or resume evidence",
    )


def _name(candidate: Candidate) -> str:
    return f"{candidate.first_name} {candidate.last_name}".strip()


async def _job_in_org(session: AsyncSession, org_id: UUID, job_id: UUID) -> Job | None:
    return await session.scalar(select(Job).where(Job.id == job_id, Job.org_id == org_id))


async def _application_in_org(
    session: AsyncSession, org_id: UUID, application_id: UUID
) -> Application | None:
    return await session.scalar(
        select(Application).where(
            Application.id == application_id, Application.org_id == org_id
        )
    )


async def list_jobs(session, org_id, actor_user_id, args: ListJobsArgs) -> dict:
    stmt = select(Job).where(Job.org_id == org_id)
    if args.query:
        stmt = stmt.where(Job.title.ilike(f"%{args.query}%"))
    rows = (await session.scalars(stmt.order_by(Job.created_at.desc()).limit(args.limit))).all()

    return {
        "jobs": [
            {
                "job_id": str(job.id),
                "title": job.title,
                "dimensions": [d.get("dimension") for d in (job.rubric or [])],
                "shortlist_rules": job.shortlist_rules or {},
                "created_at": job.created_at,
            }
            for job in rows
        ]
    }


async def rank_candidates_for_job(
    session, org_id, actor_user_id, args: RankCandidatesArgs
) -> dict:
    job = await _job_in_org(session, org_id, args.job_id)
    if job is None:
        return {"error": "not_found", "detail": "no such job in this organization"}

    rules = rules_of(job)
    rows = (await session.execute(ranked_select(org_id, job.id).limit(200))).all()

    candidates = []
    for application, resume_score, call_score, overall, rank in rows:
        if args.stage and application.stage != args.stage:
            continue
        candidates.append(
            {
                "application_id": str(application.id),
                "candidate_name": _name(application.candidate),
                "stage": application.stage,
                "rank": rank,
                "resume_score": resume_score,
                "call_score": call_score,
                "ranked_by": overall,
                "qualifies": qualifies(rules, rank, overall),
            }
        )
        if len(candidates) >= args.limit:
            break

    return {"job_title": job.title, "shortlist_rules": rules.model_dump(), "candidates": candidates}


async def search_candidates(session, org_id, actor_user_id, args: SearchCandidatesArgs) -> dict:
    needle = f"%{args.query}%"
    stmt = (
        select(Application)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(
            Application.org_id == org_id,
            or_(
                Candidate.first_name.ilike(needle),
                Candidate.last_name.ilike(needle),
                Candidate.email.ilike(needle),
            ),
        )
    )
    if args.job_id is not None:
        stmt = stmt.where(Application.job_id == args.job_id)

    rows = (
        await session.scalars(stmt.order_by(Application.created_at.desc()).limit(args.limit))
    ).all()

    return {
        "matches": [
            {
                "application_id": str(a.id),
                "candidate_name": _name(a.candidate),
                "candidate_email": a.candidate.email,
                "job_title": a.job.title,
                "stage": a.stage,
            }
            for a in rows
        ]
    }


async def get_candidate_profile(session, org_id, actor_user_id, args: ApplicationArgs) -> dict:
    application = await _application_in_org(session, org_id, args.application_id)
    if application is None:
        return {"error": "not_found", "detail": "no such application in this organization"}

    resume = await session.scalar(
        select(Resume)
        .where(
            Resume.org_id == org_id,
            Resume.candidate_id == application.candidate_id,
            Resume.status == READY,
        )
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .limit(1)
    )
    scores = (
        await session.scalars(
            select(Score)
            .where(Score.org_id == org_id, Score.application_id == application.id)
            .order_by(Score.created_at.desc())
        )
    ).all()

    latest: dict[str, dict] = {}
    for score in scores:
        latest.setdefault(score.stage, score.breakdown)

    return {
        "application_id": str(application.id),
        "candidate_name": _name(application.candidate),
        "job_title": application.job.title,
        "stage": application.stage,
        "resume_status": resume.status if resume else "awaiting_resume",
        "resume": (resume.data or {}) if resume else None,
        "resume_score": latest.get(RESUME_STAGE),
        "call_score": latest.get(CALL_STAGE),
    }


async def get_screening_summary(session, org_id, actor_user_id, args: ApplicationArgs) -> dict:
    application = await _application_in_org(session, org_id, args.application_id)
    if application is None:
        return {"error": "not_found", "detail": "no such application in this organization"}

    call = await session.scalar(
        select(Call)
        .where(Call.org_id == org_id, Call.application_id == application.id)
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    if call is None:
        return {"screened": False, "detail": "this candidate has not been screened yet"}

    summary = await session.scalar(
        select(CallSummary).where(CallSummary.org_id == org_id, CallSummary.call_id == call.id)
    )
    if summary is None:
        return {"screened": True, "call_status": call.status, "summary": None}

    return {
        "screened": True,
        "call_status": call.status,
        "summary": summary.summary,
        "rubric_scores": summary.rubric_scores,
        "facts": summary.facts,
        "red_flags": summary.red_flags,
        "confidence": summary.confidence,
    }


async def explain_ranking(session, org_id, actor_user_id, args: ApplicationArgs) -> dict:
    profile = await get_candidate_profile(session, org_id, actor_user_id, args)
    if "error" in profile:
        return profile

    application = await _application_in_org(session, org_id, args.application_id)
    assert application is not None
    job = await _job_in_org(session, org_id, application.job_id)

    screening = await get_screening_summary(session, org_id, actor_user_id, args)

    return {
        **profile,
        "rubric": (job.rubric if job else []),
        "shortlist_rules": (rules_of(job).model_dump() if job else {}),
        "screening": screening,
    }


async def draft_interview_questions(
    session, org_id, actor_user_id, args: DraftQuestionsArgs
) -> dict:
    from core.job_drafter import draft_job
    from core.models import Organization
    from core.providers import ProviderNotConfigured, resolve_org_llm

    organization = await session.get(Organization, org_id)
    llm = None
    if organization is not None:
        try:
            llm = await resolve_org_llm(session, organization)
        except ProviderNotConfigured:
            llm = None

    try:
        draft = await draft_job(args.title, args.description, llm)
    except Exception as exc:
        return {"error": "draft_failed", "detail": str(exc)[:200]}

    return {
        "title": draft.title,
        "rubric": draft.rubric.model_dump(mode="json"),
        "question_plan": draft.question_plan.model_dump(mode="json"),
        "note": "Nothing was saved. Offer the user the option to create a job from this draft.",
    }


async def propose_screening(session, org_id, actor_user_id, args: ProposeScreeningArgs) -> dict:
    rows = (
        await session.scalars(
            select(Application).where(
                Application.org_id == org_id, Application.id.in_(args.application_ids)
            )
        )
    ).all()
    found = {application.id: application for application in rows}

    entries = []
    eligible_ids = []
    for application_id in args.application_ids:
        application = found.get(application_id)
        if application is None:
            entries.append(
                {
                    "application_id": str(application_id),
                    "candidate_name": None,
                    "eligible": False,
                    "reason": "no such application in this organization",
                }
            )
            continue

        resume = await session.scalar(
            select(Resume)
            .where(
                Resume.org_id == org_id,
                Resume.candidate_id == application.candidate_id,
                Resume.status == READY,
            )
            .limit(1)
        )
        live = await session.scalar(
            select(Call)
            .where(
                Call.org_id == org_id,
                Call.application_id == application.id,
                Call.status.in_(LIVE_CALL_STATUSES),
            )
            .limit(1)
        )

        reason = None
        if resume is None:
            reason = "no extracted resume yet"
        elif live is not None:
            reason = "a screening is already in progress"

        entries.append(
            {
                "application_id": str(application.id),
                "candidate_name": _name(application.candidate),
                "eligible": reason is None,
                "reason": reason,
            }
        )
        if reason is None:
            eligible_ids.append(str(application.id))

    if not eligible_ids:
        return {
            "status": "nothing_to_propose",
            "applications": entries,
            "note": "No candidate here can be screened right now. Tell the user why.",
        }

    conversation_id = _conversation_id.get()
    action = AssistantPendingAction(
        id=uuid.uuid4(),
        org_id=org_id,
        conversation_id=conversation_id,
        action="start_screening",
        payload={"application_ids": eligible_ids, "reason": args.reason},
        status="pending",
    )
    session.add(action)
    session.add(
        AuditEvent(
            org_id=org_id,
            actor_user_id=actor_user_id,
            entity_type="assistant_action",
            entity_id=action.id,
            event_type="assistant_action_proposed",
            payload={
                "action": "start_screening",
                "application_ids": eligible_ids,
                "conversation_id": str(conversation_id),
                "reason": args.reason,
            },
        )
    )
    await session.flush()

    return {
        "pending_action_id": str(action.id),
        "status": "awaiting_user_confirmation",
        "applications": entries,
        "note": (
            "Nothing has been sent. Tell the user what you propose and ask them to confirm "
            "in the card below."
        ),
    }


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool(
            name="list_jobs",
            description="List the open roles in this organization, with their rubric dimensions "
            "and any shortlist rules.",
            args=ListJobsArgs,
            permission=Permission.JOB_READ,
            run=list_jobs,
        ),
        Tool(
            name="rank_candidates_for_job",
            description="Rank a job's candidates best-first. Ranked by the post-call score when "
            "a candidate has been screened, otherwise by their resume score.",
            args=RankCandidatesArgs,
            permission=Permission.SCORE_READ,
            run=rank_candidates_for_job,
        ),
        Tool(
            name="search_candidates",
            description="Find candidates by a fragment of their name or email. This is a literal "
            "substring match, not a search by skill or experience.",
            args=SearchCandidatesArgs,
            permission=Permission.CANDIDATE_READ,
            run=search_candidates,
        ),
        Tool(
            name="get_candidate_profile",
            description="One candidate's extracted resume and their scores for this application.",
            args=ApplicationArgs,
            permission=Permission.CANDIDATE_READ,
            run=get_candidate_profile,
        ),
        Tool(
            name="get_screening_summary",
            description="The summary, rubric scores and red flags from a candidate's screening "
            "call, if they have had one.",
            args=ApplicationArgs,
            permission=Permission.SCORE_READ,
            run=get_screening_summary,
        ),
        Tool(
            name="explain_ranking",
            description="Everything behind one candidate's position: their resume, scores, the "
            "job's rubric and shortlist rules, and their screening summary.",
            args=ApplicationArgs,
            permission=Permission.SCORE_READ,
            run=explain_ranking,
        ),
        Tool(
            name="draft_interview_questions",
            description="Draft a scoring rubric and a screening question plan from a pasted job "
            "description. Nothing is saved; the user chooses whether to create a job from it.",
            args=DraftQuestionsArgs,
            permission=Permission.JOB_WRITE,
            run=draft_interview_questions,
        ),
        Tool(
            name="propose_screening",
            description="Propose screening one or more candidates. This only records a proposal "
            "for the user to confirm — it never sends anything.",
            args=ProposeScreeningArgs,
            permission=Permission.CALL_CREATE,
            run=propose_screening,
        ),
    ]
}


def tool_schemas() -> list[ChatCompletionToolParam]:
    return [
        cast(
            ChatCompletionToolParam,
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.args.model_json_schema(),
                },
            },
        )
        for tool in TOOLS.values()
    ]
