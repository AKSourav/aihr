import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import WorkflowAlreadyStartedError
from test_read_api import auth_header, client, make_member  # noqa: F401

from core.models import (
    Application,
    AssistantConversation,
    AssistantMessage,
    AssistantPendingAction,
    AuditEvent,
    Candidate,
    Resume,
)
from core.settings import get_settings


@pytest.fixture(autouse=True)
def email_configured(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("INVITE_SIGNING_KEY", "k" * 48)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeTemporal:
    def __init__(self, already: set[str] | None = None) -> None:
        self.started: list[str] = []
        self.already = already or set()

    async def start_workflow(self, workflow, arg, id, task_queue):
        if id in self.already:
            raise WorkflowAlreadyStartedError(id, workflow)
        self.started.append(id)


@pytest.fixture
def temporal(monkeypatch) -> FakeTemporal:
    fake = FakeTemporal()

    async def fake_client():
        return fake

    monkeypatch.setattr("services.api.routes.screenings.temporal_client", fake_client)
    return fake


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
    if ready:
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
                text="resume text",
            )
        )
    await session.flush()
    return application


@pytest.fixture
async def proposal(session: AsyncSession, two_tenants):
    async def build(user_sub_id, applications, created_at=None):
        conversation = AssistantConversation(
            id=uuid.uuid4(),
            org_id=two_tenants["org_a"],
            user_id=user_sub_id,
            title="chat",
        )
        session.add(conversation)
        await session.flush()

        action = AssistantPendingAction(
            id=uuid.uuid4(),
            org_id=two_tenants["org_a"],
            conversation_id=conversation.id,
            action="start_screening",
            payload={
                "application_ids": [str(a.id) for a in applications],
                "reason": "strong resumes",
            },
            status="pending",
            created_at=created_at or datetime.now(timezone.utc),
        )
        session.add(action)
        await session.flush()
        return action

    return build


async def user_id_for(session: AsyncSession, sub: str):
    from core.models import User

    return await session.scalar(select(User.id).where(User.keycloak_sub == sub))


async def test_confirming_starts_the_screenings(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    applications = [
        await make_applicant(session, two_tenants["org_a"], two_tenants["job_a"], "Asha"),
        await make_applicant(session, two_tenants["org_a"], two_tenants["job_a"], "Priya"),
    ]
    action = await proposal(user_id, applications)

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert [r["status"] for r in body["results"]] == ["started", "started"]
    assert len(temporal.started) == 2

    await session.refresh(action)
    assert action.status == "confirmed"
    assert action.resolved_at is not None


async def test_nothing_starts_until_the_user_confirms(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    await proposal(user_id, [application])

    assert temporal.started == []


async def test_cancelling_starts_nothing(client, session, two_tenants, proposal, temporal):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    action = await proposal(user_id, [application])

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": False},
        headers=auth_header(sub),
    )

    assert response.json()["status"] == "cancelled"
    assert temporal.started == []

    cancelled = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.org_id == two_tenants["org_a"],
            AuditEvent.event_type == "assistant_action_cancelled",
        )
    )
    assert cancelled == 1


async def test_confirming_twice_is_a_conflict(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    action = await proposal(user_id, [application])
    url = f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm"

    first = await client.post(url, json={"confirm": True}, headers=auth_header(sub))
    second = await client.post(url, json={"confirm": True}, headers=auth_header(sub))

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(temporal.started) == 1


async def test_an_expired_proposal_is_a_conflict(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    action = await proposal(user_id, [application], created_at=stale)

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(sub),
    )

    assert response.status_code == 409
    assert temporal.started == []
    await session.refresh(action)
    assert action.status == "expired"


async def test_a_different_user_may_not_confirm(
    client, session, two_tenants, proposal, temporal
):
    owner_sub = await make_member(session, two_tenants["org_a"], "recruiter")
    other_sub = await make_member(session, two_tenants["org_a"], "recruiter")
    owner_id = await user_id_for(session, owner_sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    action = await proposal(owner_id, [application])

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(other_sub),
    )

    assert response.status_code == 404
    assert temporal.started == []


async def test_a_viewer_may_not_confirm(client, session, two_tenants, proposal, temporal):
    viewer_sub = await make_member(session, two_tenants["org_a"], "viewer")
    viewer_id = await user_id_for(session, viewer_sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    action = await proposal(viewer_id, [application])

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(viewer_sub),
    )

    assert response.status_code == 403
    assert temporal.started == []


async def test_one_failure_does_not_abort_the_batch(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    good = await make_applicant(session, two_tenants["org_a"], two_tenants["job_a"], "Asha")
    busy = await make_applicant(session, two_tenants["org_a"], two_tenants["job_a"], "Priya")
    temporal.already.add(f"call-{busy.job_id}-{busy.candidate_id}")
    action = await proposal(user_id, [good, busy])

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    statuses = {r["application_id"]: r["status"] for r in response.json()["results"]}
    assert statuses[str(good.id)] == "started"
    assert statuses[str(busy.id)] == "already_running"
    assert len(temporal.started) == 1


async def test_a_confirmed_batch_appends_an_assistant_message(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    action = await proposal(user_id, [application])

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(sub),
    )

    message = await session.scalar(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == action.conversation_id,
            AssistantMessage.role == "assistant",
        )
    )
    assert message is not None
    assert "Started 1 of 1" in (message.content or "")


async def test_an_assistant_screening_is_audited_as_such(
    client, session, two_tenants, proposal, temporal
):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    user_id = await user_id_for(session, sub)
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    action = await proposal(user_id, [application])

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/actions/{action.id}/confirm",
        json={"confirm": True},
        headers=auth_header(sub),
    )

    started = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_id == application.id,
            AuditEvent.event_type == "screening_started",
        )
    )
    assert started is not None
    assert started.payload["source"] == "assistant"
    assert started.payload["pending_action_id"] == str(action.id)
