import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Application, AuditEvent, Candidate, Job, LlmUsage, Resume, Score
from core.schemas import ResumeScore, RubricScore, TokenUsage

RUBRIC = [
    {"dimension": "backend_depth", "weight": 0.75, "description": "builds backends"},
    {"dimension": "ownership", "weight": 0.25, "description": "drives work end to end"},
]


@pytest.fixture
def activity(monkeypatch):
    """Import the activity lazily so monkeypatching its module-level names is easy."""
    import services.temporal_worker.activities.score_resume as module

    def install(scores: dict, evidence: str = "led the payments migration"):
        async def fake(job_title, rubric, text, data=None, llm=None):
            return (
                ResumeScore(
                    rubric_scores=[
                        RubricScore(dimension=name, score=value, evidence=evidence)
                        for name, value in scores.items()
                    ],
                    confidence="high",
                ),
                TokenUsage(prompt_tokens=200, completion_tokens=50),
            )

        monkeypatch.setattr(module, "run_scorer", fake)
        return module

    return install


@pytest.fixture
async def scored_job(session: AsyncSession, two_tenants):
    job = await session.get(Job, two_tenants["job_a"])
    assert job is not None
    job.rubric = RUBRIC
    await session.flush()
    return job


async def make_applicant(session: AsyncSession, org_id, job_id, name: str) -> Application:
    candidate = Candidate(
        id=uuid.uuid4(),
        org_id=org_id,
        first_name=name,
        last_name="Person",
        email=f"{name.lower()}-{uuid.uuid4().hex}@candidates.test",
    )
    session.add(candidate)
    await session.flush()

    application = Application(
        id=uuid.uuid4(),
        org_id=org_id,
        candidate_id=candidate.id,
        job_id=job_id,
        stage="shortlisted",
    )
    session.add(application)
    session.add(
        Resume(
            id=uuid.uuid4(),
            org_id=org_id,
            candidate_id=candidate.id,
            job_id=job_id,
            pdf_url="s3://test-bucket/r.pdf",
            filename="r.pdf",
            byte_size=10,
            status="ready",
            text="led the payments migration at Acme",
            data={"skills": ["Python"]},
        )
    )
    await session.flush()
    return application


async def run_activity(module, session, application_id):
    """The activity opens its own session; point it at the test one."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session():
        yield session

    original = module.async_session
    module.async_session = fake_session
    try:
        from services.temporal_worker.shared import ScoreResumeInput

        await module.score_resume(ScoreResumeInput(application_id=str(application_id)))
    finally:
        module.async_session = original


async def test_scoring_writes_a_resume_score_and_usage(
    session: AsyncSession, two_tenants, scored_job, activity
):
    module = activity({"backend_depth": 0.8, "ownership": 0.4})
    application = await make_applicant(
        session, two_tenants["org_a"], scored_job.id, "Asha"
    )

    await run_activity(module, session, application.id)

    score = await session.scalar(
        select(Score).where(Score.application_id == application.id, Score.stage == "initial")
    )
    assert score is not None
    assert score.breakdown["overall"] == 0.7
    assert score.breakdown["backend_depth"] == 0.8
    assert score.breakdown["confidence"] == "high"

    usage = await session.scalar(
        select(LlmUsage).where(LlmUsage.application_id == application.id)
    )
    assert usage is not None
    assert usage.call_id is None
    assert usage.prompt_tokens == 200


async def test_evidence_mentioning_a_protected_term_is_blanked_and_flagged(
    session: AsyncSession, two_tenants, scored_job, activity
):
    module = activity(
        {"backend_depth": 0.8, "ownership": 0.4}, evidence="returned from maternity leave"
    )
    application = await make_applicant(
        session, two_tenants["org_a"], scored_job.id, "Priya"
    )

    await run_activity(module, session, application.id)

    score = await session.scalar(
        select(Score).where(Score.application_id == application.id, Score.stage == "initial")
    )
    assert score is not None
    assert score.breakdown["evidence"]["backend_depth"] == ""
    assert score.breakdown["backend_depth"] == 0.8

    flagged = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == application.id,
            AuditEvent.event_type == "resume_score_flagged",
        )
    )
    assert flagged == 1


async def test_scoring_advances_a_candidate_that_meets_the_rules(
    session: AsyncSession, two_tenants, scored_job, activity
):
    scored_job.shortlist_rules = {"min_score": 0.6, "auto_advance": True}
    await session.flush()
    module = activity({"backend_depth": 0.8, "ownership": 0.8})
    application = await make_applicant(
        session, two_tenants["org_a"], scored_job.id, "Strong"
    )

    await run_activity(module, session, application.id)

    await session.refresh(application)
    assert application.stage == "qualified"


async def test_scoring_leaves_a_candidate_below_the_rules_alone(
    session: AsyncSession, two_tenants, scored_job, activity
):
    scored_job.shortlist_rules = {"min_score": 0.9, "auto_advance": True}
    await session.flush()
    module = activity({"backend_depth": 0.2, "ownership": 0.2})
    application = await make_applicant(
        session, two_tenants["org_a"], scored_job.id, "Weak"
    )

    await run_activity(module, session, application.id)

    await session.refresh(application)
    assert application.stage == "shortlisted"


async def test_an_application_without_a_ready_resume_is_skipped(
    session: AsyncSession, two_tenants, scored_job, activity
):
    module = activity({"backend_depth": 0.8, "ownership": 0.8})
    candidate = Candidate(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        first_name="NoResume",
        last_name="Person",
        email=f"noresume-{uuid.uuid4().hex}@candidates.test",
    )
    session.add(candidate)
    await session.flush()
    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=candidate.id,
        job_id=scored_job.id,
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()

    await run_activity(module, session, application.id)

    score = await session.scalar(
        select(Score).where(Score.application_id == application.id)
    )
    assert score is None
