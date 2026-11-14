import importlib
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application, Membership, User
from core.settings import get_settings
from services.api import auth

TOKEN = "integration-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def recruiter(session: AsyncSession, two_tenants):
    user = User(id=uuid.uuid4(), keycloak_sub=TOKEN, email="recruiter@acme.test")
    session.add(user)
    await session.flush()
    session.add(
        Membership(
            id=uuid.uuid4(), user_id=user.id, org_id=two_tenants["org_a"], role="recruiter"
        )
    )
    await session.flush()
    return user


@pytest.fixture
async def client(monkeypatch, session: AsyncSession, recruiter):
    monkeypatch.setenv("DEV_ROUTES_ENABLED", "true")
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    monkeypatch.setattr(auth, "decode_token", lambda token: {"sub": token, "email": "r@acme.test"})
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
