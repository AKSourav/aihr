import importlib
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, Organization, User
from core.settings import get_settings
from services.api import auth


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


async def make_user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(), keycloak_sub=str(uuid.uuid4()), email=f"{uuid.uuid4()}@acme.test"
    )
    session.add(user)
    await session.flush()
    return user


def header(user: User) -> dict:
    return {"Authorization": f"Bearer {user.keycloak_sub}"}


async def test_a_user_with_no_memberships_can_found_a_tenant(client, session):
    user = await make_user(session)

    response = await client.post(
        "/organizations", headers=header(user), json={"name": "Acme Fintech"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme Fintech"
    assert body["role"] == "owner"

    organization = await session.get(Organization, uuid.UUID(body["org_id"]))
    assert str(organization.account_id) == body["account_id"]
    membership = await session.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.org_id == organization.id
        )
    )
    assert membership.role == "owner"


async def test_the_founder_appears_in_me_as_an_owner(client, session):
    user = await make_user(session)

    created = await client.post(
        "/organizations", headers=header(user), json={"name": "Acme Fintech"}
    )
    me = await client.get("/me", headers=header(user))

    assert me.status_code == 200
    memberships = me.json()["memberships"]
    assert len(memberships) == 1
    assert memberships[0]["org_id"] == created.json()["org_id"]
    assert memberships[0]["account_id"] == created.json()["account_id"]
    assert memberships[0]["role"] == "owner"
    assert "tenant:create" in memberships[0]["permissions"]


async def test_the_founder_may_invite_into_their_new_tenant(client, session, monkeypatch):
    from services.api import keycloak_admin

    async def find_user_by_email(c, email):
        return None

    async def create_user(c, email):
        return str(uuid.uuid4())

    async def send_invitation_email(c, subject):
        return True

    monkeypatch.setattr(keycloak_admin, "find_user_by_email", find_user_by_email)
    monkeypatch.setattr(keycloak_admin, "create_user", create_user)
    monkeypatch.setattr(keycloak_admin, "send_invitation_email", send_invitation_email)

    user = await make_user(session)
    created = (
        await client.post("/organizations", headers=header(user), json={"name": "Acme Fintech"})
    ).json()

    response = await client.post(
        f"/accounts/{created['account_id']}/invitations",
        headers=header(user),
        json={
            "email": f"{uuid.uuid4()}@acme.test",
            "memberships": [{"org_id": created["org_id"], "role": "recruiter"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["memberships"] == [
        {"org_id": created["org_id"], "role": "recruiter"}
    ]


async def test_one_person_may_found_several_tenants(client, session):
    user = await make_user(session)

    first = await client.post("/organizations", headers=header(user), json={"name": "Acme One"})
    second = await client.post("/organizations", headers=header(user), json={"name": "Acme Two"})
    me = await client.get("/me", headers=header(user))

    assert first.status_code == second.status_code == 201
    assert first.json()["account_id"] != second.json()["account_id"]
    assert len(me.json()["memberships"]) == 2


async def test_founding_requires_a_token(client, session):
    name = f"Acme {uuid.uuid4()}"

    response = await client.post("/organizations", json={"name": name})

    assert response.status_code == 401
    assert await session.scalar(select(Organization).where(Organization.name == name)) is None


async def test_a_blank_name_is_rejected(client, session):
    user = await make_user(session)

    response = await client.post("/organizations", headers=header(user), json={"name": ""})

    assert response.status_code == 422
