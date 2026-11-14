import importlib
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application, Call
from core.models import CallSummary as CallSummaryRow
from core.models import Membership, Score, User
from core.settings import get_settings
from services.api import auth


def auth_header(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture
async def seeded(session: AsyncSession, two_tenants):
    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()

    call = Call(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        application_id=application.id,
        call_mode="web",
        status="completed",
        duration_seconds=312,
    )
    session.add(call)
    await session.flush()

    session.add(
        Score(
            id=uuid.uuid4(),
            org_id=two_tenants["org_a"],
            application_id=application.id,
            stage="post_call",
            breakdown={"overall": 0.82, "backend_depth": 0.9},
        )
    )
    session.add(
        CallSummaryRow(
            id=uuid.uuid4(),
            org_id=two_tenants["org_a"],
            call_id=call.id,
            summary="Strong backend candidate.",
            rubric_scores=[{"dimension": "backend_depth", "score": 0.9, "evidence": "led migration"}],
            facts={"notice_period_days": 60},
            red_flags=[],
            candidate_questions=[],
            confidence="high",
            model_version="test-model",
            prompt_version="v2",
        )
    )
    await session.flush()
    return {"application": application, "call": call}


async def make_member(session: AsyncSession, org_id, role: str) -> str:
    sub = str(uuid.uuid4())
    user = User(id=uuid.uuid4(), keycloak_sub=sub, email=f"{role}@acme.test")
    session.add(user)
    await session.flush()
    session.add(Membership(id=uuid.uuid4(), user_id=user.id, org_id=org_id, role=role))
    await session.flush()
    return sub


@pytest.fixture
async def client(monkeypatch, session: AsyncSession):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "tok")
    monkeypatch.setattr(auth, "decode_token", lambda token: {"sub": token, "email": "x@acme.test"})
    get_settings.cache_clear()

    import services.api.main as api_main

    app = importlib.reload(api_main).app
    app.dependency_overrides[get_session] = lambda: session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


async def test_viewer_can_list_applications_with_scores(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/applications", headers=auth_header(sub)
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["overall_score"] == 0.82
    assert body[0]["stage"] == "shortlisted"
    assert body[0]["candidate_email"] == "asha@candidates.test"


async def test_applications_from_another_tenant_are_invisible(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_b"], "admin")

    response = await client.get(
        f"/tenants/{two_tenants['org_b']}/applications", headers=auth_header(sub)
    )

    assert response.status_code == 200
    assert response.json() == []


async def test_application_detail_includes_scores_and_calls(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/applications/{seeded['application'].id}",
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["scores"]) == 1
    assert len(body["calls"]) == 1
    assert body["calls"][0]["duration_seconds"] == 312


async def test_reading_another_tenants_application_by_id_is_404(
    client, session, two_tenants, seeded
):
    sub = await make_member(session, two_tenants["org_b"], "admin")

    response = await client.get(
        f"/tenants/{two_tenants['org_b']}/applications/{seeded['application'].id}",
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_passing_another_tenants_id_in_the_path_is_404(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_b"], "admin")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/applications/{seeded['application'].id}",
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_viewer_can_read_a_summary(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/calls/{seeded['call'].id}/summary",
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    assert response.json()["confidence"] == "high"
    assert response.json()["prompt_version"] == "v2"


async def test_viewer_may_not_read_a_transcript(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/calls/{seeded['call'].id}/transcript",
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_recruiter_may_attempt_a_transcript_read(client, session, two_tenants, seeded):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.get(
        f"/tenants/{two_tenants['org_a']}/calls/{seeded['call'].id}/transcript",
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_a_non_member_sees_404_on_every_read(client, session, two_tenants, seeded):
    sub = str(uuid.uuid4())
    session.add(User(id=uuid.uuid4(), keycloak_sub=sub, email="nobody@acme.test"))
    await session.flush()

    for path in (
        f"/tenants/{two_tenants['org_a']}/applications",
        f"/tenants/{two_tenants['org_a']}/applications/{seeded['application'].id}",
        f"/tenants/{two_tenants['org_a']}/calls/{seeded['call'].id}",
    ):
        response = await client.get(path, headers=auth_header(sub))
        assert response.status_code == 404, path
