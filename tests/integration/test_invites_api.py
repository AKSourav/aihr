import importlib
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import WorkflowAlreadyStartedError

from core.db import get_session
from core.invites import issue_invite_token
from core.models import Application, AuditEvent, Call, Membership, User
from core.settings import get_settings
from services.api import auth
from services.api.routes import public_invites, screenings


def auth_header(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


def in_hours(hours: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class FakeHandle:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def signal(self, name: str, arg) -> None:
        self.calls.append(("signal", name, arg))


class FakeTemporal:
    def __init__(self, already_started: bool = False) -> None:
        self.calls: list = []
        self.already_started = already_started

    async def start_workflow(self, workflow, arg, id, task_queue):
        if self.already_started:
            raise WorkflowAlreadyStartedError(id, workflow)
        self.calls.append(("start", workflow, arg, id, task_queue))

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        self.calls.append(("handle", workflow_id))
        return FakeHandle(self.calls)


@pytest.fixture
def temporal() -> FakeTemporal:
    return FakeTemporal()


@pytest.fixture
def dispatched(monkeypatch) -> list:
    rooms: list = []

    async def fake_dispatch(call_id):
        rooms.append(str(call_id))
        return f"call-{call_id}"

    monkeypatch.setattr(public_invites, "create_room_and_dispatch", fake_dispatch)
    monkeypatch.setattr(public_invites, "candidate_join_token", lambda room, name: f"lk-token:{room}:{name}")
    return rooms


@pytest.fixture
async def client(monkeypatch, session: AsyncSession, temporal: FakeTemporal):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "tok")
    monkeypatch.setenv("INVITE_SIGNING_KEY", "k" * 32)
    monkeypatch.setenv("SMTP_HOST", "mailpit")
    monkeypatch.setenv("LIVEKIT_URL", "wss://lk.example")
    monkeypatch.setattr(auth, "decode_token", lambda token: {"sub": token, "email": "x@acme.test"})

    async def fake_client():
        return temporal

    monkeypatch.setattr(public_invites, "temporal_client", fake_client)
    monkeypatch.setattr(screenings, "temporal_client", fake_client)
    get_settings.cache_clear()

    import services.api.main as api_main

    app = importlib.reload(api_main).app
    app.dependency_overrides[get_session] = lambda: session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
async def application(session: AsyncSession, two_tenants) -> Application:
    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()
    return application


async def make_call(session: AsyncSession, application: Application, **fields) -> Call:
    values = dict(
        id=uuid.uuid4(),
        org_id=application.org_id,
        application_id=application.id,
        call_mode="email",
        status="invited",
        invite_sent_at=datetime.now(timezone.utc),
        invite_expires_at=in_hours(72),
    )
    values.update(fields)
    call = Call(**values)
    session.add(call)
    await session.flush()
    return call


async def make_member(session: AsyncSession, org_id, role: str) -> str:
    sub = str(uuid.uuid4())
    user = User(id=uuid.uuid4(), keycloak_sub=sub, email=f"{role}-{uuid.uuid4()}@acme.test")
    session.add(user)
    await session.flush()
    session.add(Membership(id=uuid.uuid4(), user_id=user.id, org_id=org_id, role=role))
    await session.flush()
    return sub


def token_for(call: Call, hours: float = 72) -> str:
    return issue_invite_token(call.id, in_hours(hours))


async def test_invite_details_are_shown_to_the_candidate(client, session, application):
    call = await make_call(session, application)

    response = await client.get(f"/public/invites/{token_for(call)}")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["candidate_first_name"] == "Asha"
    assert body["job_title"] == "A job"


async def test_joining_dispatches_the_agent_once_and_signals_the_workflow(
    client, session, application, dispatched, temporal
):
    call = await make_call(session, application)
    token = token_for(call)

    first = await client.post(f"/public/invites/{token}/join")
    second = await client.post(f"/public/invites/{token}/join")

    assert first.status_code == 200
    assert first.json() == {
        "livekit_url": "wss://lk.example",
        "token": f"lk-token:call-{call.id}:Asha",
        "room_name": f"call-{call.id}",
    }
    assert second.status_code == 200, "a reload rejoins the same room"
    assert dispatched == [str(call.id)]

    await session.refresh(call)
    assert call.status == "in_progress"
    assert call.room_name == f"call-{call.id}"
    signals = [c for c in temporal.calls if c[0] == "signal"]
    assert signals == [("signal", "call_joined", str(call.id))]


async def test_a_finished_call_cannot_be_joined(client, session, application, dispatched):
    call = await make_call(session, application, status="completed")

    response = await client.post(f"/public/invites/{token_for(call)}/join")

    assert response.status_code == 409
    assert dispatched == []


async def new_application(session: AsyncSession, two_tenants) -> Application:
    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()
    return application


async def test_an_expired_invite_is_gone(client, session, two_tenants, dispatched):
    # Each on its own application, so there is no newer invite to forward to.
    expired_row = await make_call(session, await new_application(session, two_tenants), status="expired")
    past_deadline = await make_call(
        session, await new_application(session, two_tenants), invite_expires_at=in_hours(-1)
    )
    expired_token = await make_call(session, await new_application(session, two_tenants))

    assert (await client.post(f"/public/invites/{token_for(expired_row)}/join")).status_code == 410
    assert (await client.post(f"/public/invites/{token_for(past_deadline)}/join")).status_code == 410
    assert (await client.post(f"/public/invites/{token_for(expired_token, -1)}/join")).status_code == 410
    assert dispatched == []


async def test_an_old_expired_link_takes_the_candidate_to_the_current_invite(
    client, session, application, dispatched, temporal
):
    first = await make_call(session, application, attempt_no=1, status="expired", invite_expires_at=in_hours(-1))
    reminder = await make_call(session, application, attempt_no=2)
    old_link = token_for(first, -1)

    info = await client.get(f"/public/invites/{old_link}")
    joined = await client.post(f"/public/invites/{old_link}/join")

    assert info.status_code == 200
    assert info.json()["state"] == "ready"
    assert info.json()["forwarded"] is True
    assert joined.status_code == 200
    assert joined.json()["room_name"] == f"call-{reminder.id}"
    assert dispatched == [str(reminder.id)], "the agent goes to the current call only"
    signals = [c for c in temporal.calls if c[0] == "signal"]
    assert signals == [("signal", "call_joined", str(reminder.id))]

    await session.refresh(first)
    await session.refresh(reminder)
    assert first.status == "expired" and first.room_name is None
    assert reminder.status == "in_progress"
    audit = await session.scalar(
        select(AuditEvent).where(AuditEvent.entity_id == reminder.id, AuditEvent.event_type == "invite_joined")
    )
    assert audit is not None
    assert audit.payload == {"via_link_for_call_id": str(first.id)}


async def test_a_link_from_before_a_callback_request_forwards(client, session, application, dispatched):
    earlier = await make_call(session, application, attempt_no=1, status="callback_requested")
    current = await make_call(session, application, attempt_no=2)

    response = await client.post(f"/public/invites/{token_for(earlier)}/join")

    assert response.status_code == 200
    assert dispatched == [str(current.id)]


async def test_an_old_link_rejoins_the_call_already_in_progress(client, session, application, dispatched):
    first = await make_call(session, application, attempt_no=1, status="expired", invite_expires_at=in_hours(-1))
    live = await make_call(session, application, attempt_no=2, status="in_progress", room_name="call-live")

    response = await client.post(f"/public/invites/{token_for(first, -1)}/join")

    assert response.status_code == 200
    assert response.json()["room_name"] == "call-live"
    assert dispatched == [], "the agent is already in that room"
    await session.refresh(live)
    assert live.status == "in_progress"


async def test_the_current_link_is_not_marked_forwarded(client, session, application):
    await make_call(session, application, attempt_no=1, status="expired", invite_expires_at=in_hours(-1))
    current = await make_call(session, application, attempt_no=2)

    response = await client.get(f"/public/invites/{token_for(current)}")

    assert response.json()["forwarded"] is False


async def test_an_old_link_with_nothing_newer_keeps_its_own_answer(client, session, two_tenants, dispatched):
    expired = await make_call(
        session, await new_application(session, two_tenants), status="expired", invite_expires_at=in_hours(-1)
    )
    finished = await make_call(session, await new_application(session, two_tenants), status="completed")

    assert (await client.get(f"/public/invites/{token_for(expired, -1)}")).status_code == 410
    assert (await client.get(f"/public/invites/{token_for(finished)}")).json()["state"] == "finished"
    assert (await client.post(f"/public/invites/{token_for(finished)}/join")).status_code == 409
    assert dispatched == []


async def test_an_expired_newer_invite_is_not_a_target(client, session, application, dispatched):
    first = await make_call(session, application, attempt_no=1, status="expired", invite_expires_at=in_hours(-80))
    await make_call(session, application, attempt_no=2, invite_expires_at=in_hours(-1))

    assert (await client.post(f"/public/invites/{token_for(first, -80)}/join")).status_code == 410
    assert dispatched == []


async def test_links_never_cross_applications(client, session, two_tenants, application, dispatched):
    first = await make_call(session, application, status="expired", invite_expires_at=in_hours(-1))
    other_application = await new_application(session, two_tenants)
    await make_call(session, other_application)

    assert (await client.post(f"/public/invites/{token_for(first, -1)}/join")).status_code == 410
    assert dispatched == []


async def test_a_link_long_past_expiry_no_longer_forwards(client, session, application, dispatched):
    first = await make_call(session, application, attempt_no=1, status="expired", invite_expires_at=in_hours(-31 * 24))
    await make_call(session, application, attempt_no=2)

    assert (await client.post(f"/public/invites/{token_for(first, -31 * 24)}/join")).status_code == 410
    assert dispatched == []


async def test_a_token_for_a_non_email_call_is_not_found(client, session, application, dispatched):
    call = await make_call(session, application, call_mode="web", status="ringing")

    assert (await client.get(f"/public/invites/{token_for(call)}")).status_code == 404
    assert (await client.post(f"/public/invites/{token_for(call)}/join")).status_code == 404


async def test_recruiter_starts_an_email_screening(client, session, two_tenants, application, temporal):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/applications/{application.id}/screenings",
        json={"channel": "email"},
        headers=auth_header(sub),
    )

    assert response.status_code == 202
    workflow_id = f"call-{application.job_id}-{application.candidate_id}"
    assert response.json() == {"workflow_id": workflow_id, "channel": "email"}
    (start,) = [c for c in temporal.calls if c[0] == "start"]
    assert start[1] == "ScreeningCallWorkflow"
    assert start[2]["call_mode"] == "email"
    assert start[2]["application_id"] == str(application.id)
    assert start[3] == workflow_id
    assert start[4] == "voice"

    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_id == application.id, AuditEvent.event_type == "screening_started"
        )
    )
    assert audit is not None


async def test_a_running_screening_is_a_conflict(client, session, two_tenants, application, temporal):
    temporal.already_started = True
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/applications/{application.id}/screenings",
        json={"channel": "email"},
        headers=auth_header(sub),
    )

    assert response.status_code == 409


async def test_phone_screenings_are_not_available_yet(client, session, two_tenants, application, temporal):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/applications/{application.id}/screenings",
        json={"channel": "phone"},
        headers=auth_header(sub),
    )

    assert response.status_code == 501
    assert temporal.calls == []


async def test_viewer_cannot_start_a_screening(client, session, two_tenants, application, temporal):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/applications/{application.id}/screenings",
        json={"channel": "email"},
        headers=auth_header(sub),
    )

    assert response.status_code == 403
    assert temporal.calls == []


async def test_an_application_in_another_tenant_is_not_found(
    client, session, two_tenants, application, temporal
):
    sub = await make_member(session, two_tenants["org_b"], "admin")

    response = await client.post(
        f"/tenants/{two_tenants['org_b']}/applications/{application.id}/screenings",
        json={"channel": "email"},
        headers=auth_header(sub),
    )

    assert response.status_code == 404
    assert temporal.calls == []


async def test_screenings_need_email_configured(
    client, monkeypatch, session, two_tenants, application, temporal
):
    monkeypatch.setenv("SMTP_HOST", "")
    get_settings.cache_clear()
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/applications/{application.id}/screenings",
        json={"channel": "email"},
        headers=auth_header(sub),
    )

    assert response.status_code == 503
    assert temporal.calls == []


async def test_me_lists_memberships_with_permissions(client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.get("/me", headers=auth_header(sub))

    assert response.status_code == 200
    body = response.json()
    assert [m["org_id"] for m in body["memberships"]] == [str(two_tenants["org_a"])]
    assert body["memberships"][0]["role"] == "viewer"
    assert "score:read" in body["memberships"][0]["permissions"]
    assert "transcript:read" not in body["memberships"][0]["permissions"]


async def test_me_for_a_user_without_memberships_is_empty(client):
    response = await client.get("/me", headers=auth_header(str(uuid.uuid4())))

    assert response.status_code == 200
    assert response.json()["memberships"] == []
