import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_read_api import (  # noqa: F401
    auth_header,
    client,
    extraction,
    make_member,
    resume_file,
)

from core.models import Application, Candidate, Job
from core.permissions import Permission, grants
from core.settings import get_settings


def job_payload(title: str) -> dict:
    return {
        "title": title,
        "rubric": [
            {"dimension": "backend_depth", "weight": 0.6, "description": "Designs backend services"},
            {"dimension": "communication", "weight": 0.4, "description": "Explains clearly"},
        ],
        "question_plan": {
            "intro_pitch": "Acme builds payment infrastructure.",
            "questions": [
                {"id": "q_project", "text": "Describe your last project.", "required": True}
            ],
            "max_minutes": 15,
        },
    }


async def count_jobs(session: AsyncSession, org_id, title: str) -> int | None:
    return await session.scalar(
        select(func.count()).select_from(Job).where(Job.org_id == org_id, Job.title == title)
    )


async def count_candidates(session: AsyncSession, org_id, domain: str) -> int | None:
    return await session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.org_id == org_id, Candidate.email.like(f"%@{domain}"))
    )


def test_only_owner_and_admin_may_write_jobs():
    assert grants("owner", Permission.JOB_WRITE)
    assert grants("admin", Permission.JOB_WRITE)
    assert not grants("recruiter", Permission.JOB_WRITE)
    assert not grants("viewer", Permission.JOB_WRITE)


async def test_owner_creates_a_job_and_sees_it_listed(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    title = f"Staff Engineer {uuid.uuid4()}"

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs",
        json=job_payload(title),
        headers=auth_header(sub),
    )

    assert response.status_code == 201
    job_id = response.json()["job_id"]
    assert response.json()["title"] == title

    listing = await client.get(f"/tenants/{two_tenants['org_a']}/jobs", headers=auth_header(sub))
    assert listing.status_code == 200
    assert job_id in [job["job_id"] for job in listing.json()]

    detail = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{job_id}", headers=auth_header(sub)
    )
    assert detail.status_code == 200
    assert detail.json()["rubric"] == job_payload(title)["rubric"]
    assert detail.json()["question_plan"]["max_minutes"] == 15


async def test_a_malformed_rubric_is_rejected_and_writes_nothing(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    title = f"Bad rubric {uuid.uuid4()}"
    body = job_payload(title)
    body["rubric"] = [
        {"dimension": "depth", "weight": 0.5, "description": "a"},
        {"dimension": "depth", "weight": 0.5, "description": "b"},
    ]

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs", json=body, headers=auth_header(sub)
    )

    assert response.status_code == 422
    assert await count_jobs(session, two_tenants["org_a"], title) == 0


async def test_a_zero_weight_rubric_dimension_is_rejected(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    title = f"Zero weight {uuid.uuid4()}"
    body = job_payload(title)
    body["rubric"] = [{"dimension": "depth", "weight": 0, "description": "a"}]

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs", json=body, headers=auth_header(sub)
    )

    assert response.status_code == 422
    assert await count_jobs(session, two_tenants["org_a"], title) == 0


async def test_a_malformed_question_plan_is_rejected(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    title = f"Bad plan {uuid.uuid4()}"
    body = job_payload(title)
    body["question_plan"]["questions"] = []

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs", json=body, headers=auth_header(sub)
    )

    assert response.status_code == 422
    assert await count_jobs(session, two_tenants["org_a"], title) == 0


async def test_a_viewer_may_not_create_a_job(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs",
        json=job_payload(f"Nope {uuid.uuid4()}"),
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_creating_a_job_in_another_org_is_404(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    title = f"Trespass {uuid.uuid4()}"

    response = await client.post(
        f"/tenants/{two_tenants['org_b']}/jobs",
        json=job_payload(title),
        headers=auth_header(sub),
    )

    assert response.status_code == 404
    assert await count_jobs(session, two_tenants["org_b"], title) == 0


async def test_drafting_without_an_llm_configured_is_503(
    monkeypatch, client, session, two_tenants
):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "")
    get_settings.cache_clear()
    sub = await make_member(session, two_tenants["org_a"], "owner")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/drafts",
        json={"title": "Staff Engineer", "description": "We need a backend engineer."},
        headers=auth_header(sub),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "AI drafting is not configured on this server"


async def test_a_viewer_may_not_draft_a_job(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/jobs/drafts",
        json={"title": "Staff Engineer", "description": "We need a backend engineer."},
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_adding_a_candidate_creates_an_application(
    client, session, two_tenants, extraction
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    email = f"{uuid.uuid4().hex}@candidates.test"

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data={
            "first_name": "Nina",
            "last_name": "Iyer",
            "email": email,
            "job_id": str(two_tenants["job_a"]),
        },
        files=resume_file(),
        headers=auth_header(sub),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["application_id"] is not None
    assert body["resume_id"] is not None
    assert extraction.started == [f"resume-{body['resume_id']}"]

    application = await session.scalar(
        select(Application).where(Application.id == uuid.UUID(body["application_id"]))
    )
    assert application is not None
    assert application.stage == "shortlisted"
    assert application.org_id == two_tenants["org_a"]


async def test_a_duplicate_email_reuses_the_candidate(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    email = f"{uuid.uuid4().hex}@candidates.test"
    payload = {"first_name": "Nina", "last_name": "Iyer", "email": email}

    first = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data=payload,
        files=resume_file(),
        headers=auth_header(sub),
    )
    second = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data={**payload, "first_name": "Renamed"},
        files=resume_file(),
        headers=auth_header(sub),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["created"] is False
    assert second.json()["candidate_id"] == first.json()["candidate_id"]

    candidate = await session.scalar(
        select(Candidate).where(Candidate.id == uuid.UUID(first.json()["candidate_id"]))
    )
    assert candidate is not None
    assert candidate.first_name == "Nina"


async def test_a_job_id_from_another_org_is_404(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    email = f"{uuid.uuid4().hex}@candidates.test"

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data={
            "first_name": "Nina",
            "last_name": "Iyer",
            "email": email,
            "job_id": str(two_tenants["job_b"]),
        },
        files=resume_file(),
        headers=auth_header(sub),
    )

    assert response.status_code == 404
    assert await session.scalar(select(Candidate).where(Candidate.email == email)) is None


async def test_creating_an_application_with_another_tenants_candidate_is_404(
    client, session, two_tenants
):
    sub = await make_member(session, two_tenants["org_b"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_b']}/applications",
        json={
            "candidate_id": str(two_tenants["candidate_a"]),
            "job_id": str(two_tenants["job_b"]),
        },
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_creating_an_application_returns_the_application_shape(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/applications",
        json={
            "candidate_id": str(two_tenants["candidate_a"]),
            "job_id": str(two_tenants["job_a"]),
        },
        headers=auth_header(sub),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["candidate_name"] == "Asha Rao"
    assert body["job_title"] == "A job"
    assert body["stage"] == "shortlisted"
    assert body["overall_score"] is None


async def test_importing_three_rows_then_re_importing_reports_duplicates(
    client, session, two_tenants
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    domain = f"{uuid.uuid4().hex}.test"
    payload = {
        "job_id": str(two_tenants["job_a"]),
        "rows": [
            {
                "first_name": f"First{index}",
                "last_name": "Row",
                "email": f"row{index}@{domain}",
                "phone_e164": f"+9100000000{index}",
            }
            for index in range(3)
        ],
    }

    first = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json=payload,
        headers=auth_header(sub),
    )

    assert first.status_code == 200
    assert first.json()["created"] == 3
    assert [row["status"] for row in first.json()["results"]] == ["created"] * 3
    assert await count_candidates(session, two_tenants["org_a"], domain) == 3

    second = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json=payload,
        headers=auth_header(sub),
    )

    assert second.status_code == 200
    assert second.json()["duplicates"] == 3
    assert second.json()["created"] == 0
    assert [row["status"] for row in second.json()["results"]] == ["duplicate"] * 3
    assert await count_candidates(session, two_tenants["org_a"], domain) == 3


async def test_importing_against_another_orgs_job_is_404(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    domain = f"{uuid.uuid4().hex}.test"

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json={
            "job_id": str(two_tenants["job_b"]),
            "rows": [{"first_name": "A", "last_name": "B", "email": f"a@{domain}"}],
        },
        headers=auth_header(sub),
    )

    assert response.status_code == 404
    assert await count_candidates(session, two_tenants["org_a"], domain) == 0


async def test_a_mixed_import_reports_every_status(client, session, two_tenants, extraction):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    domain = f"{uuid.uuid4().hex}.test"
    known = f"known@{domain}"
    fresh = f"fresh@{domain}"

    await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates",
        data={"first_name": "Known", "last_name": "Person", "email": known},
        files=resume_file(),
        headers=auth_header(sub),
    )

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json={
            "job_id": str(two_tenants["job_a"]),
            "rows": [
                {"first_name": "Known", "last_name": "Person", "email": known},
                {"first_name": "Fresh", "last_name": "Person", "email": fresh},
                {"first_name": "Fresh", "last_name": "Person", "email": f"  {fresh.upper()}  "},
                {"first_name": "  ", "last_name": "Person", "email": f"blank@{domain}"},
                {"first_name": "Bad", "last_name": "Email", "email": "not-an-email"},
            ],
        },
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["status"] for row in body["results"]] == [
        "matched",
        "created",
        "duplicate",
        "invalid",
        "invalid",
    ]
    assert (body["created"], body["matched"], body["duplicates"], body["invalid"]) == (1, 1, 1, 2)
    assert body["results"][3]["row"] == 4
    assert body["results"][4]["reason"] is not None
    assert await count_candidates(session, two_tenants["org_a"], domain) == 2


async def test_an_invalid_row_does_not_reduce_the_committed_count(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    domain = f"{uuid.uuid4().hex}.test"

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json={
            "job_id": str(two_tenants["job_a"]),
            "rows": [
                {"first_name": "One", "last_name": "Row", "email": f"one@{domain}"},
                {"first_name": "Bad", "last_name": "Row", "email": "nope"},
                {"first_name": "Two", "last_name": "Row", "email": f"two@{domain}"},
            ],
        },
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    assert response.json()["created"] == 2
    assert response.json()["invalid"] == 1
    assert await count_candidates(session, two_tenants["org_a"], domain) == 2


async def test_structural_garbage_is_a_422(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    not_a_list = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json={"job_id": str(two_tenants["job_a"]), "rows": {"email": "a@b.test"}},
        headers=auth_header(sub),
    )
    bad_job_id = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json={"job_id": "not-a-uuid", "rows": []},
        headers=auth_header(sub),
    )
    too_many = await client.post(
        f"/tenants/{two_tenants['org_a']}/candidates/import",
        json={
            "job_id": str(two_tenants["job_a"]),
            "rows": [
                {"first_name": "A", "last_name": "B", "email": f"r{index}@bulk.test"}
                for index in range(501)
            ],
        },
        headers=auth_header(sub),
    )

    assert not_a_list.status_code == 422
    assert bad_job_id.status_code == 422
    assert too_many.status_code == 422
