import importlib
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application
from core.settings import get_settings

TOKEN = "integration-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client(monkeypatch, session: AsyncSession):
    monkeypatch.setenv("DEV_ROUTES_ENABLED", "true")
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
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
async def application_in_org_a(session: AsyncSession, two_tenants):
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


async def test_unknown_tenant_returns_404(client, application_in_org_a):
    response = await client.post(
        f"/tenants/{uuid.uuid4()}/calls", json={"candidate_key": "asha"}, headers=AUTH
    )

    assert response.status_code == 404


async def test_candidate_from_another_tenant_returns_404(client, two_tenants, application_in_org_a):
    response = await client.post(
        f"/tenants/{two_tenants['org_b']}/calls", json={"candidate_key": "asha"}, headers=AUTH
    )

    assert response.status_code == 404


async def test_call_is_created_within_its_tenant(client, two_tenants, application_in_org_a):
    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/calls", json={"candidate_key": "asha"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["application_id"] == str(application_in_org_a.id)
