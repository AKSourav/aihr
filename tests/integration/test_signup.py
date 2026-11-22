import importlib
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Account, Membership, Organization, User
from core.settings import get_settings
from services.api import auth, keycloak_admin


@pytest.fixture
def keycloak(monkeypatch):
    calls = {"created": [], "emailed": [], "existing": None, "fail": False}

    async def find_user_by_email(client, email):
        return calls["existing"]

    async def create_user(client, email):
        if calls["fail"]:
            raise keycloak_admin.KeycloakError("boom")
        subject = str(uuid.uuid4())
        calls["created"].append((email, subject))
        return subject

    async def send_invitation_email(client, subject):
        calls["emailed"].append(subject)
        return True

    monkeypatch.setattr(keycloak_admin, "find_user_by_email", find_user_by_email)
    monkeypatch.setattr(keycloak_admin, "create_user", create_user)
    monkeypatch.setattr(keycloak_admin, "send_invitation_email", send_invitation_email)
    return calls


@pytest.fixture
async def client(monkeypatch, session: AsyncSession):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "tok")
    monkeypatch.setattr(
        auth, "decode_token", lambda token: {"sub": token, "email": f"{token}@acme.test"}
    )
    get_settings.cache_clear()

    import services.api.main as api_main

    app = importlib.reload(api_main).app
    app.dependency_overrides[get_session] = lambda: session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


async def sole_membership(session: AsyncSession, email: str) -> Membership:
    user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    rows = (await session.scalars(select(Membership).where(Membership.user_id == user.id))).all()
    assert len(rows) == 1
    return rows[0]


async def test_signup_creates_the_identity_account_tenant_and_owner(client, session, keycloak):
    email = f"{uuid.uuid4()}@acme.test"

    response = await client.post(
        "/signup", json={"email": email, "organization_name": "Acme Fintech"}
    )

    assert response.status_code == 202
    assert response.json() == {"status": "verification_sent"}
    assert keycloak["created"][0][0] == email
    assert keycloak["emailed"] == [keycloak["created"][0][1]]

    membership = await sole_membership(session, email)
    assert membership.role == "owner"
    organization = await session.get(Organization, membership.org_id)
    assert organization.name == "Acme Fintech"
    assert await session.get(Account, organization.account_id) is not None


async def test_signup_is_rejected_without_a_tenant_name(client, session, keycloak):
    response = await client.post("/signup", json={"email": f"{uuid.uuid4()}@acme.test"})

    assert response.status_code == 422
    assert not keycloak["created"]


async def test_a_known_address_is_accepted_without_creating_anything(client, session, keycloak):
    email = f"{uuid.uuid4()}@acme.test"
    name = f"Acme {uuid.uuid4()}"
    session.add(User(id=uuid.uuid4(), keycloak_sub=str(uuid.uuid4()), email=email))
    await session.flush()

    response = await client.post("/signup", json={"email": email, "organization_name": name})

    assert response.status_code == 202
    assert response.json() == {"status": "verification_sent"}
    assert not keycloak["created"]
    rows = (await session.scalars(select(Organization).where(Organization.name == name))).all()
    assert rows == []


async def test_an_address_known_only_to_keycloak_creates_nothing(client, session, keycloak):
    email = f"{uuid.uuid4()}@acme.test"
    keycloak["existing"] = {"id": str(uuid.uuid4()), "emailVerified": True}

    response = await client.post(
        "/signup", json={"email": email, "organization_name": "Acme Fintech"}
    )

    assert response.status_code == 202
    assert not keycloak["created"]
    assert await session.scalar(select(User).where(User.email == email)) is None


async def test_a_keycloak_failure_leaves_no_local_state(client, session, keycloak):
    email = f"{uuid.uuid4()}@acme.test"
    keycloak["fail"] = True

    response = await client.post(
        "/signup", json={"email": email, "organization_name": "Acme Fintech"}
    )

    assert response.status_code == 502
    assert await session.scalar(select(User).where(User.email == email)) is None


async def test_two_signups_may_name_their_tenant_the_same(client, session, keycloak):
    first = f"{uuid.uuid4()}@acme.test"
    second = f"{uuid.uuid4()}@acme.test"
    name = f"Acme {uuid.uuid4()}"

    one = await client.post("/signup", json={"email": first, "organization_name": name})
    two = await client.post("/signup", json={"email": second, "organization_name": name})

    assert one.status_code == two.status_code == 202
    a = await sole_membership(session, first)
    b = await sole_membership(session, second)
    assert a.org_id != b.org_id

    orgs = (await session.scalars(select(Organization).where(Organization.name == name))).all()
    assert len({org.account_id for org in orgs}) == 2
