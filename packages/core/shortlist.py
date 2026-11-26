from uuid import UUID

from sqlalchemy import desc, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from core.models import Application, AuditEvent, Job, Score
from core.schemas import ShortlistRules

RESUME_STAGE = "initial"
CALL_STAGE = "post_call"

QUALIFIED_STAGE = "qualified"
ADVANCEABLE_FROM = frozenset({"shortlisted"})
MAX_RANKED = 1000


def _latest(org_id: UUID, stage: str, name: str):
    return (
        select(Score.application_id.label("application_id"), Score.breakdown.label("breakdown"))
        .where(Score.org_id == org_id, Score.stage == stage)
        .distinct(Score.application_id)
        .order_by(Score.application_id, Score.created_at.desc(), Score.id.desc())
        .subquery(name)
    )


def ranked_select(org_id: UUID, job_id: UUID):
    resume = _latest(org_id, RESUME_STAGE, "resume_score")
    call = _latest(org_id, CALL_STAGE, "call_score")
    resume_overall = resume.c.breakdown["overall"].as_float()
    call_overall = call.c.breakdown["overall"].as_float()
    ranking = func.coalesce(call_overall, resume_overall)
    order = [nulls_last(desc(ranking)), Application.created_at.asc(), Application.id.asc()]

    return (
        select(
            Application,
            resume_overall.label("resume_score"),
            call_overall.label("call_score"),
            ranking.label("overall"),
            func.row_number().over(order_by=order).label("rank"),
        )
        .outerjoin(resume, resume.c.application_id == Application.id)
        .outerjoin(call, call.c.application_id == Application.id)
        .where(Application.org_id == org_id, Application.job_id == job_id)
        .order_by(*order)
    )


def rules_of(job: Job) -> ShortlistRules:
    return ShortlistRules.model_validate(job.shortlist_rules or {})


def qualifies(rules: ShortlistRules, rank: int, overall: float | None) -> bool:
    checks = []
    if rules.top_k is not None:
        checks.append(rank <= rules.top_k)
    if rules.min_score is not None:
        checks.append(overall is not None and overall >= rules.min_score)
    if not checks:
        return False
    return all(checks) if rules.combine == "all" else any(checks)


async def apply_rules(
    session: AsyncSession, job: Job, actor_user_id: UUID | None = None
) -> list[UUID]:
    rules = rules_of(job)
    if not rules.auto_advance or (rules.top_k is None and rules.min_score is None):
        return []

    rows = (
        await session.execute(ranked_select(job.org_id, job.id).limit(MAX_RANKED))
    ).all()
    winners = [
        application.id
        for application, _resume, _call, overall, rank in rows
        if qualifies(rules, rank, overall)
    ]
    if not winners:
        return []

    locked = (
        await session.scalars(
            select(Application)
            .options(noload(Application.candidate), noload(Application.job))
            .where(
                Application.org_id == job.org_id,
                Application.id.in_(winners),
                Application.stage.in_(ADVANCEABLE_FROM),
            )
            .with_for_update()
        )
    ).all()

    for application in locked:
        session.add(
            AuditEvent(
                org_id=job.org_id,
                actor_user_id=actor_user_id,
                entity_type="application",
                entity_id=application.id,
                event_type="stage_advanced",
                payload={
                    "from": application.stage,
                    "to": QUALIFIED_STAGE,
                    "rules": rules.model_dump(exclude_none=True),
                },
            )
        )
        application.stage = QUALIFIED_STAGE

    return [application.id for application in locked]
