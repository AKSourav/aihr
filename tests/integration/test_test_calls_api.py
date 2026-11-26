import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_read_api import auth_header, client, make_member  # noqa: F401

from core.invites import verify_invite_token
from core.models import Application, AuditEvent, Call, Candidate
from core.settings import get_settings


@pytest.fixture(autouse=True)
def voice_configured(monkeypatch):
    monkeypatch.setenv("INVITE_SIGNING_KEY", "k" * 48)
    monkeypatch.setenv("LIVEKIT_URL", "wss://livekit.test")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_recruiter_can_start_a_self_test_call(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_a"])},
        headers=auth_header(sub),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["join_url"].startswith("http://localhost:8000/join/")

    token = body["join_url"].rsplit("/", 1)[-1]
    assert str(verify_invite_token(token)) == body["call_id"]

    call = await session.get(Call, body["call_id"])
    assert call is not None
    assert call.status == "invited"
    assert call.call_mode == "web"


async def test_the_self_test_candidate_belongs_to_the_caller(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_a"])},
        headers=auth_header(sub),
    )

    application = await session.get(Application, response.json()["application_id"])
    assert application is not None
    assert application.stage == "self_test"

    candidate = await session.get(Candidate, application.candidate_id)
    assert candidate is not None
    assert candidate.email.startswith("selftest-")
    assert candidate.email.endswith("@candidates.test")


async def test_starting_a_second_test_call_expires_the_first(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    url = f"/tenants/{two_tenants['org_a']}/test-calls"
    payload = {"job_id": str(two_tenants["job_a"])}

    first = await client.post(url, json=payload, headers=auth_header(sub))
    second = await client.post(url, json=payload, headers=auth_header(sub))

    assert first.json()["call_id"] != second.json()["call_id"]

    old = await session.get(Call, first.json()["call_id"])
    new = await session.get(Call, second.json()["call_id"])
    assert old is not None and old.status == "expired"
    assert new is not None and new.status == "invited"
    assert new.attempt_no == 2


async def test_a_test_call_does_not_need_a_resume(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_a"])},
        headers=auth_header(sub),
    )

    assert response.status_code == 201


async def test_a_viewer_may_not_start_a_test_call(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_a"])},
        headers=auth_header(sub),
    )

    assert response.status_code == 403


async def test_a_test_call_against_another_tenants_job_is_404(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_b"])},
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_a_test_call_is_audited(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "owner")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_a"])},
        headers=auth_header(sub),
    )

    events = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == response.json()["call_id"],
            AuditEvent.event_type == "test_call_started",
        )
    )
    assert events == 1


async def test_a_test_call_is_503_when_voice_is_unconfigured(
    client, session, two_tenants, monkeypatch
):
    monkeypatch.setenv("LIVEKIT_URL", "")
    get_settings.cache_clear()
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/test-calls",
        json={"job_id": str(two_tenants["job_a"])},
        headers=auth_header(sub),
    )

    assert response.status_code == 503
