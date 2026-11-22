import importlib
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application, Call, Job
from core.settings import get_settings
from services.api.routes import internal_calls

INTERNAL_TOKEN = "internal-tok"
AGENT_TOKEN = "agent-tok"

QUESTION_PLAN = {
    "intro_pitch": "Quick chat about the role.",
    "questions": [{"id": "q1", "text": "Tell me about your last project.", "must_ask": True}],
    "max_minutes": 15,
}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class FakeStorage:
    def upload_json(self, key: str, data) -> str:
        return f"s3://test/{key}"


@pytest.fixture
async def client(monkeypatch, session: AsyncSession, request):
    monkeypatch.setenv("INTERNAL_API_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("AGENT_API_TOKEN", getattr(request, "param", AGENT_TOKEN))
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DEEPGRAM_API_KEY", "env-stt-key")
    monkeypatch.setenv("TTS_API_KEY", "env-tts-key")
    monkeypatch.setenv("LLM_API_KEY", "env-llm-key")
    get_settings.cache_clear()

    monkeypatch.setattr(internal_calls, "Storage", FakeStorage)

    async def fake_client():
        raise RuntimeError("no temporal in tests")

    monkeypatch.setattr(internal_calls, "temporal_client", fake_client)

    import services.api.main as api_main

    app = importlib.reload(api_main).app
    app.dependency_overrides[get_session] = lambda: session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
async def call(session: AsyncSession, two_tenants) -> Call:
    job = await session.get(Job, two_tenants["job_a"])
    assert job is not None
    job.question_plan = QUESTION_PLAN

    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()

    row = Call(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        application_id=application.id,
        call_mode="web",
        status="in_progress",
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_live_call_issues_providers_to_the_agent_token(client, call):
    response = await client.get(f"/internal/calls/{call.id}/providers", headers=bearer(AGENT_TOKEN))

    assert response.status_code == 200
    assert response.json()["stt"]["api_key"] == "env-stt-key"


async def test_providers_reject_the_internal_token(client, call):
    response = await client.get(f"/internal/calls/{call.id}/providers", headers=bearer(INTERNAL_TOKEN))

    assert response.status_code == 401


async def test_context_still_accepts_the_internal_token(client, call):
    response = await client.get(f"/internal/calls/{call.id}/context", headers=bearer(INTERNAL_TOKEN))

    assert response.status_code == 200
    assert response.json()["call_id"] == str(call.id)


async def test_context_rejects_the_agent_token(client, call):
    response = await client.get(f"/internal/calls/{call.id}/context", headers=bearer(AGENT_TOKEN))

    assert response.status_code == 401


async def test_complete_still_accepts_the_internal_token(client, call, session: AsyncSession):
    response = await client.post(
        f"/internal/calls/{call.id}/complete",
        headers=bearer(INTERNAL_TOKEN),
        json={"call_id": str(call.id), "status": "completed", "transcript": []},
    )

    assert response.status_code == 200
    await session.refresh(call)
    assert call.status == "completed"


@pytest.mark.parametrize("client", [""], indirect=True)
async def test_an_empty_agent_token_rejects_everything(client, call):
    for header in (bearer(""), {"Authorization": "Bearer "}, bearer(AGENT_TOKEN), {}):
        response = await client.get(f"/internal/calls/{call.id}/providers", headers=header)
        assert response.status_code == 401


async def test_a_finished_call_is_refused_providers(client, call, session: AsyncSession):
    call.status = "completed"
    await session.flush()

    response = await client.get(f"/internal/calls/{call.id}/providers", headers=bearer(AGENT_TOKEN))

    assert response.status_code == 409


async def test_a_stale_call_is_refused_providers(client, call, session: AsyncSession):
    stale = datetime.now(timezone.utc) - timedelta(minutes=get_settings().max_call_duration_min + 5)
    call.updated_at = stale
    await session.flush()

    response = await client.get(f"/internal/calls/{call.id}/providers", headers=bearer(AGENT_TOKEN))

    assert response.status_code == 409
