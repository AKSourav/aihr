import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_read_api import auth_header, client, make_member  # noqa: F401

from core.models import Application, AuditEvent, Candidate, Resume
from core.schemas import CohortNote, CohortSummary, TokenUsage
from core.settings import get_settings


@pytest.fixture(autouse=True)
def llm_configured(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "test-model")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_cohort(monkeypatch):
    seen: dict = {}

    async def fake(job_title, rubric, candidates, llm=None):
        seen["candidates"] = candidates
        return (
            CohortSummary(
                summary="Two candidates.",
                candidates=[CohortNote(name=name, strengths=[], gaps=[]) for name, _ in candidates],
                recommendation="Screen the first.",
            ),
            TokenUsage(prompt_tokens=100, completion_tokens=40),
        )

    monkeypatch.setattr("services.api.routes.jobs.summarize_cohort", fake)
    return seen


async def make_applicant(session: AsyncSession, org_id, job_id, name: str, ready: bool = True):
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
            status="ready" if ready else "extracting",
            text="resume text" if ready else None,
            data={"skills": ["Python"]},
        )
    )
    await session.flush()
    return application


@pytest.fixture
async def cohort(session: AsyncSession, two_tenants):
    org, job = two_tenants["org_a"], two_tenants["job_a"]
    return [
        await make_applicant(session, org, job, "Asha"),
        await make_applicant(session, org, job, "Vikram"),
    ]


async def test_a_recruiter_can_summarize_several_candidates(
    client, session, two_tenants, cohort, fake_cohort
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(a.id) for a in cohort]},
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "Two candidates."
    assert {c["name"] for c in body["candidates"]} == {"Asha Person", "Vikram Person"}
    assert len(fake_cohort["candidates"]) == 2


async def test_fewer_than_two_candidates_is_rejected(client, session, two_tenants, cohort):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(cohort[0].id)]},
        headers=auth_header(sub),
    )

    assert response.status_code == 422


async def test_more_candidates_than_the_cap_are_rejected(client, session, two_tenants, cohort):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(uuid.uuid4()) for _ in range(11)]},
        headers=auth_header(sub),
    )

    assert response.status_code == 422


async def test_an_application_from_another_job_is_rejected(
    client, session, two_tenants, cohort, fake_cohort
):
    other = await make_applicant(
        session, two_tenants["org_b"], two_tenants["job_b"], "Outsider"
    )
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(cohort[0].id), str(other.id)]},
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_a_candidate_without_an_extracted_resume_is_a_conflict(
    client, session, two_tenants, cohort, fake_cohort
):
    pending = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Pending", ready=False
    )
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(cohort[0].id), str(pending.id)]},
        headers=auth_header(sub),
    )

    assert response.status_code == 409
    assert "Pending Person" in response.json()["detail"]


async def test_a_viewer_may_not_generate_a_summary(client, session, two_tenants, cohort):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(a.id) for a in cohort]},
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_a_summary_is_audited(client, session, two_tenants, cohort, fake_cohort):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidate-summary",
        json={"application_ids": [str(a.id) for a in cohort]},
        headers=auth_header(sub),
    )

    events = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == two_tenants["job_a"],
            AuditEvent.event_type == "candidate_summary_generated",
        )
    )
    assert events == 1
