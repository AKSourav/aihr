import asyncio
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import async_session
from core.models import Application, Candidate, CandidateProfile, Job, Organization, Score, User

ORG_NAME = "Acme Fintech"
HR_EMAIL = "hr@acme.test"

SENIOR_BACKEND_JOB = {
    "title": "Senior Backend Engineer",
    "rubric": [
        {"dimension": "backend_depth", "weight": 0.35, "description": "Designing and operating backend services at scale"},
        {"dimension": "system_design", "weight": 0.25, "description": "Reasoning about trade-offs in distributed systems"},
        {"dimension": "communication", "weight": 0.2, "description": "Clear, structured explanations"},
        {"dimension": "role_fit", "weight": 0.2, "description": "Notice period, location, compensation alignment"},
    ],
    "question_plan": {
        "intro_pitch": "Acme Fintech builds payment infrastructure for SMBs in India.",
        "questions": [
            {"id": "q_recent_project", "text": "Tell me about the most complex backend system you've worked on recently.", "probe": "Ask about scale and their specific role.", "required": True},
            {"id": "q_incident", "text": "Describe a production incident you handled.", "probe": "Ask what they changed afterwards.", "required": True},
            {"id": "q_notice", "text": "What is your current notice period?", "required": True, "extract": "notice_period_days"},
            {"id": "q_ctc", "text": "What are your current and expected compensation?", "required": True, "extract": ["current_ctc", "expected_ctc"]},
            {"id": "q_location", "text": "The role is hybrid in Bengaluru. Does that work for you?", "required": True, "extract": "open_to_location"},
        ],
        "faq": [
            {"q": "Is the role remote?", "a": "Hybrid, three days a week in the Bengaluru office."},
            {"q": "What's the interview process?", "a": "A technical round, a system design round, and a final conversation with the hiring manager."},
        ],
        "max_minutes": 15,
    },
}

CUSTOMER_SUPPORT_JOB = {
    "title": "Customer Support Lead",
    "rubric": [
        {"dimension": "customer_empathy", "weight": 0.4, "description": "Handling frustrated customers calmly and clearly"},
        {"dimension": "process_rigor", "weight": 0.3, "description": "Following and improving support playbooks"},
        {"dimension": "communication", "weight": 0.3, "description": "Clear, structured explanations"},
    ],
    "question_plan": {
        "intro_pitch": "Acme Fintech builds payment infrastructure for SMBs in India.",
        "questions": [
            {"id": "q_recent_escalation", "text": "Tell me about a time you handled a difficult customer escalation.", "required": True},
            {"id": "q_team_lead", "text": "Have you led a support team before?", "required": True, "extract": "has_led_team"},
            {"id": "q_notice", "text": "What is your current notice period?", "required": True, "extract": "notice_period_days"},
        ],
        "faq": [
            {"q": "Is the role remote?", "a": "Hybrid, three days a week in the Bengaluru office."},
        ],
        "max_minutes": 15,
    },
}

SDE1_JOB = {
    "title": "SDE 1",
    "rubric": [
        {"dimension": "backend_depth", "weight": 0.35, "description": "Building and operating event-driven backend services in Python/Go/Java"},
        {"dimension": "distributed_systems", "weight": 0.25, "description": "Experience with workflow orchestration, queues, and multi-tenant systems"},
        {"dimension": "communication", "weight": 0.2, "description": "Clear, structured explanations"},
        {"dimension": "role_fit", "weight": 0.2, "description": "Notice period, location, compensation alignment"},
    ],
    "question_plan": {
        "intro_pitch": "Acme Fintech builds payment infrastructure for SMBs in India.",
        "questions": [
            {"id": "q_recent_project", "text": "Tell me about the most complex backend system you've worked on recently.", "probe": "Ask about scale and their specific role.", "required": True},
            {"id": "q_incident", "text": "Describe a production incident you handled.", "probe": "Ask what they changed afterwards.", "required": True},
            {"id": "q_notice", "text": "What is your current notice period?", "required": True, "extract": "notice_period_days"},
            {"id": "q_ctc", "text": "What are your current and expected compensation?", "required": True, "extract": ["current_ctc", "expected_ctc"]},
            {"id": "q_location", "text": "The role is hybrid in Bengaluru. Does that work for you?", "required": True, "extract": "open_to_location"},
        ],
        "faq": [
            {"q": "Is the role remote?", "a": "Hybrid, three days a week in the Bengaluru office."},
            {"q": "What's the interview process?", "a": "A technical round, a system design round, and a final conversation with the hiring manager."},
        ],
        "max_minutes": 15,
    },
}

RESUME_CANDIDATE = {
    "key": "sde1_resume",
    "first_name": "Anupam",
    "last_name": "Sourav",
    "summary": (
        "SDE-1 at a healthcare data platform (Python, Go, Java, PostgreSQL, Kafka, Temporal, "
        "Docker, Kubernetes, AWS): built an event-driven S3 ingestion pipeline with Lambda and "
        "Temporal, a scheduled multi-EHR data sync pipeline, per-tenant concurrency limits in a "
        "Temporal orchestrator across 15+ tenants, and automated environment provisioning cutting "
        "setup time from 1 hour to 10 minutes. Previously built FastAPI/Celery backend services "
        "and a real-time Redis pub/sub alerting system. B.Tech in Computer Science (Data Science)."
    ),
}

PERSONAS = [
    {
        "key": "strong_fit",
        "first_name": "Asha",
        "last_name": "Rao",
        "summary": "8 years backend engineering, led payments platform migration, strong system design background.",
    },
    {
        "key": "weak_fit",
        "first_name": "Vikram",
        "last_name": "Shah",
        "summary": "2 years experience, mostly CRUD apps, limited exposure to distributed systems.",
    },
    {
        "key": "busy_now",
        "first_name": "Priya",
        "last_name": "Nair",
        "summary": "6 years backend engineering, currently employed, prefers calls scheduled in advance.",
    },
    {
        "key": "chatty",
        "first_name": "Rahul",
        "last_name": "Mehta",
        "summary": "5 years backend engineering, enthusiastic communicator, tends to go on tangents.",
    },
    {
        "key": "no_consent",
        "first_name": "Sana",
        "last_name": "Khan",
        "summary": "7 years backend engineering, privacy-conscious, declines call recording.",
    },
    {
        "key": "off_topic",
        "first_name": "Karan",
        "last_name": "Verma",
        "summary": "4 years backend engineering, curious about compensation and other open roles.",
    },
]


async def upsert_org(session: AsyncSession, name: str) -> Organization:
    org = await session.scalar(select(Organization).where(Organization.name == name))
    if org is None:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
    return org


async def upsert_user(session: AsyncSession, org: Organization, email: str, role: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(org_id=org.id, email=email, role=role)
        session.add(user)
    else:
        user.role = role
    await session.flush()
    return user


async def upsert_job(session: AsyncSession, org: Organization, spec: dict) -> Job:
    job = await session.scalar(select(Job).where(Job.org_id == org.id, Job.title == spec["title"]))
    if job is None:
        job = Job(org_id=org.id, title=spec["title"], rubric=spec["rubric"], question_plan=spec["question_plan"])
        session.add(job)
    else:
        job.rubric = spec["rubric"]
        job.question_plan = spec["question_plan"]
    await session.flush()
    return job


async def upsert_candidate(
    session: AsyncSession, org: Organization, persona: dict, phone_overrides: dict, index: int
) -> Candidate:
    email = f"{persona['key']}@candidates.test"
    phone = phone_overrides.get(persona["key"], f"+91000000{index:04d}")
    now = datetime.now(timezone.utc)

    candidate = await session.scalar(select(Candidate).where(Candidate.email == email))
    if candidate is None:
        candidate = Candidate(
            org_id=org.id,
            first_name=persona["first_name"],
            last_name=persona["last_name"],
            email=email,
            phone_e164=phone,
            consent_ai_call_at=now,
            consent_recording_at=None if persona["key"] == "no_consent" else now,
        )
        session.add(candidate)
    else:
        candidate.phone_e164 = phone
    await session.flush()
    return candidate


async def upsert_profile(session: AsyncSession, candidate: Candidate, summary: str) -> CandidateProfile:
    profile = await session.scalar(select(CandidateProfile).where(CandidateProfile.candidate_id == candidate.id))
    data = {"summary": summary}
    if profile is None:
        profile = CandidateProfile(candidate_id=candidate.id, data=data)
        session.add(profile)
    else:
        profile.data = data
    await session.flush()
    return profile


async def upsert_application(session: AsyncSession, candidate: Candidate, job: Job) -> Application:
    application = await session.scalar(
        select(Application).where(Application.candidate_id == candidate.id, Application.job_id == job.id)
    )
    if application is None:
        application = Application(candidate_id=candidate.id, job_id=job.id, stage="shortlisted")
        session.add(application)
        await session.flush()
    return application


async def upsert_initial_score(session: AsyncSession, application: Application) -> Score:
    score = await session.scalar(
        select(Score).where(Score.application_id == application.id, Score.stage == "initial")
    )
    breakdown = {"overall": 0.75, "notes": "seeded placeholder score"}
    if score is None:
        score = Score(application_id=application.id, stage="initial", breakdown=breakdown)
        session.add(score)
        await session.flush()
    return score


async def seed() -> None:
    phone_overrides = json.loads(os.environ.get("SEED_PHONE_OVERRIDES", "{}"))

    async with async_session() as session:
        org = await upsert_org(session, ORG_NAME)
        await upsert_user(session, org, HR_EMAIL, "hr")

        backend_job = await upsert_job(session, org, SENIOR_BACKEND_JOB)
        await upsert_job(session, org, CUSTOMER_SUPPORT_JOB)
        sde1_job = await upsert_job(session, org, SDE1_JOB)

        for index, persona in enumerate(PERSONAS):
            candidate = await upsert_candidate(session, org, persona, phone_overrides, index)
            await upsert_profile(session, candidate, persona["summary"])
            application = await upsert_application(session, candidate, backend_job)
            await upsert_initial_score(session, application)

        resume_candidate = await upsert_candidate(session, org, RESUME_CANDIDATE, phone_overrides, len(PERSONAS))
        await upsert_profile(session, resume_candidate, RESUME_CANDIDATE["summary"])
        resume_application = await upsert_application(session, resume_candidate, sde1_job)
        await upsert_initial_score(session, resume_application)

        await session.commit()

    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
