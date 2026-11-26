import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from core.db import async_session
from core.fairness import protected_terms
from core.models import Application, AuditEvent, Job, LlmUsage, Organization, Resume, Score
from core.providers import resolve_providers
from core.resume_scorer import score_resume as run_scorer
from core.resumes import READY
from core.scoring import weighted_overall
from core.settings import get_settings
from core.shortlist import RESUME_STAGE, apply_rules
from services.temporal_worker.shared import ScoreResumeInput

logger = logging.getLogger(__name__)


async def _resume_for(
    session: AsyncSession, application: Application
) -> tuple[str, dict] | None:
    resume = await session.scalar(
        select(Resume)
        .where(
            Resume.org_id == application.org_id,
            Resume.candidate_id == application.candidate_id,
            Resume.status == READY,
        )
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .limit(1)
    )
    if resume is None or not resume.text:
        return None
    return resume.text, resume.data or {}


@activity.defn
async def score_resume(input: ScoreResumeInput) -> None:
    application_id = UUID(input.application_id)
    settings = get_settings()

    async with async_session() as session:
        application = await session.get(Application, application_id)
        if application is None:
            logger.warning("no application to score", extra={"application_id": input.application_id})
            return

        found = await _resume_for(session, application)
        if found is None:
            logger.info("no ready resume to score", extra={"application_id": input.application_id})
            return
        text, data = found

        job = await session.get(Job, application.job_id)
        if job is None:
            logger.warning("no job to score against", extra={"application_id": input.application_id})
            return

        organization = await session.get(Organization, job.org_id)
        llm = None
        if organization is not None:
            try:
                llm = (await resolve_providers(session, organization, job)).llm_summary
            except Exception as exc:
                logger.warning(
                    "falling back to platform scoring model",
                    extra={"application_id": input.application_id, "error": str(exc)},
                )

        score, usage = await run_scorer(job.title, job.rubric, text, data, llm)
        model = (llm.model if llm else "") or settings.llm_summary_model

        flagged = {}
        for rs in score.rubric_scores:
            hits = protected_terms(rs.evidence)
            if hits:
                flagged[rs.dimension] = hits
                rs.evidence = ""

        if flagged:
            logger.warning(
                "resume score flagged for review",
                extra={"application_id": input.application_id, "terms": flagged},
            )
            session.add(
                AuditEvent(
                    org_id=application.org_id,
                    entity_type="application",
                    entity_id=application.id,
                    event_type="resume_score_flagged",
                    payload={"terms": flagged},
                )
            )

        session.add(
            LlmUsage(
                org_id=application.org_id,
                call_id=None,
                application_id=application.id,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )
        )
        session.add(
            Score(
                org_id=application.org_id,
                application_id=application.id,
                stage=RESUME_STAGE,
                breakdown={
                    "overall": weighted_overall(job.rubric, score.rubric_scores),
                    **{rs.dimension: rs.score for rs in score.rubric_scores},
                    "confidence": score.confidence,
                    "evidence": {rs.dimension: rs.evidence for rs in score.rubric_scores},
                },
            )
        )
        await session.flush()
        await apply_rules(session, job)
        await session.commit()

    logger.info("resume scored", extra={"application_id": input.application_id})
