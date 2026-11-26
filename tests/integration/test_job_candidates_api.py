import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_read_api import auth_header, client, make_member  # noqa: F401

from core.models import Application, AuditEvent, Candidate, Score


async def add_candidate(session: AsyncSession, org_id, name: str) -> Candidate:
    candidate = Candidate(
        id=uuid.uuid4(),
        org_id=org_id,
        first_name=name,
        last_name="Person",
        email=f"{name.lower()}-{uuid.uuid4().hex}@candidates.test",
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def add_application(session: AsyncSession, org_id, candidate_id, job_id, created_at=None):
    application = Application(
        id=uuid.uuid4(),
        org_id=org_id,
        candidate_id=candidate_id,
        job_id=job_id,
        stage="shortlisted",
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(application)
    await session.flush()
    return application


async def add_score(session: AsyncSession, org_id, application_id, stage, overall, at=None):
    session.add(
        Score(
            id=uuid.uuid4(),
            org_id=org_id,
            application_id=application_id,
            stage=stage,
            breakdown={"overall": overall},
            created_at=at or datetime.now(timezone.utc),
        )
    )
    await session.flush()


@pytest.fixture
async def ranked(session: AsyncSession, two_tenants):
    org, job = two_tenants["org_a"], two_tenants["job_a"]
    base = datetime.now(timezone.utc)

    strong = await add_candidate(session, org, "Strong")
    middle = await add_candidate(session, org, "Middle")
    unscored = await add_candidate(session, org, "Unscored")

    a_strong = await add_application(session, org, strong.id, job, base)
    a_middle = await add_application(session, org, middle.id, job, base + timedelta(seconds=1))
    a_unscored = await add_application(session, org, unscored.id, job, base + timedelta(seconds=2))

    await add_score(session, org, a_strong.id, "initial", 0.9)
    await add_score(session, org, a_middle.id, "initial", 0.5)

    return {"strong": a_strong, "middle": a_middle, "unscored": a_unscored}


async def test_candidates_are_listed_by_score_descending(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates",
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    rows = response.json()
    assert [r["application_id"] for r in rows] == [
        str(ranked["strong"].id),
        str(ranked["middle"].id),
        str(ranked["unscored"].id),
    ]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["resume_score"] == 0.9
    assert rows[0]["call_score"] is None


async def test_candidates_without_a_score_are_listed_last(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates",
        headers=auth_header(sub),
    )

    last = response.json()[-1]
    assert last["application_id"] == str(ranked["unscored"].id)
    assert last["overall_score"] is None
    assert last["resume_score"] is None


async def test_a_post_call_score_outranks_a_resume_score(client, session, two_tenants, ranked):
    await add_score(session, two_tenants["org_a"], ranked["middle"].id, "post_call", 0.95)
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates",
        headers=auth_header(sub),
    )

    rows = response.json()
    assert rows[0]["application_id"] == str(ranked["middle"].id)
    assert rows[0]["resume_score"] == 0.5
    assert rows[0]["call_score"] == 0.95
    assert rows[0]["overall_score"] == 0.95


async def test_a_zero_post_call_score_still_outranks_a_high_resume_score(
    client, session, two_tenants, ranked
):
    await add_score(session, two_tenants["org_a"], ranked["strong"].id, "post_call", 0.0)
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates",
        headers=auth_header(sub),
    )

    rows = response.json()
    assert rows[-1]["application_id"] != str(ranked["strong"].id)
    strong = next(r for r in rows if r["application_id"] == str(ranked["strong"].id))
    assert strong["overall_score"] == 0.0
    assert strong["resume_score"] == 0.9


async def test_only_the_newest_score_of_each_stage_decides_the_order(
    client, session, two_tenants, ranked
):
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    await add_score(session, two_tenants["org_a"], ranked["middle"].id, "initial", 0.99, later)
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates",
        headers=auth_header(sub),
    )

    rows = response.json()
    assert rows[0]["application_id"] == str(ranked["middle"].id)
    assert rows[0]["resume_score"] == 0.99


async def test_the_rank_keeps_counting_on_the_second_page(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates"
        "?limit=1&offset=2",
        headers=auth_header(sub),
    )

    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["rank"] == 3
    assert rows[0]["application_id"] == str(ranked["unscored"].id)


async def test_candidates_of_another_tenants_job_are_404(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_b']}/candidates",
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_candidates_matching_the_rules_are_flagged_as_qualified(
    client, session, two_tenants, ranked
):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    await client.put(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/shortlist-rules",
        json={"rules": {"top_k": 1, "auto_advance": False}},
        headers=auth_header(sub),
    )

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/candidates",
        headers=auth_header(sub),
    )

    rows = response.json()
    assert [r["qualifies"] for r in rows] == [True, False, False]


async def test_saving_rules_advances_the_candidates_that_qualify(
    client, session, two_tenants, ranked
):
    sub = await make_member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/shortlist-rules",
        json={"rules": {"min_score": 0.8}},
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    assert response.json()["advanced"] == 1

    await session.refresh(ranked["strong"])
    await session.refresh(ranked["middle"])
    assert ranked["strong"].stage == "qualified"
    assert ranked["middle"].stage == "shortlisted"


async def test_saving_the_same_rules_again_advances_nobody_twice(
    client, session, two_tenants, ranked
):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    payload = {"rules": {"min_score": 0.8}}
    url = f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/shortlist-rules"

    first = await client.put(url, json=payload, headers=auth_header(sub))
    second = await client.put(url, json=payload, headers=auth_header(sub))

    assert first.json()["advanced"] == 1
    assert second.json()["advanced"] == 0

    events = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == ranked["strong"].id,
            AuditEvent.event_type == "stage_advanced",
        )
    )
    assert events == 1


async def test_relaxing_rules_never_demotes_anyone(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "owner")
    url = f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/shortlist-rules"

    await client.put(url, json={"rules": {"min_score": 0.8}}, headers=auth_header(sub))
    await client.put(url, json={"rules": {"min_score": 0.95}}, headers=auth_header(sub))

    await session.refresh(ranked["strong"])
    assert ranked["strong"].stage == "qualified"


async def test_a_recruiter_may_not_change_shortlist_rules(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.put(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/shortlist-rules",
        json={"rules": {"top_k": 1}},
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_a_viewer_can_read_shortlist_rules(client, session, two_tenants, ranked):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/shortlist-rules",
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    assert response.json()["rules"]["combine"] == "all"
