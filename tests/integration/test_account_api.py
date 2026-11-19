import importlib
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, User
from core.permissions import Permission
from core.settings import get_settings
from services.api import auth, keycloak_admin
from services.api.authz import require


async def make_user(session: AsyncSession, **kwargs) -> User:
    user = User(
        id=uuid.uuid4(),
        keycloak_sub=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@acme.test",
        **kwargs,
    )
    session.add(user)
    await session.flush()
    return user


async def grant(session: AsyncSession, user: User, org_id, role: str) -> None:
    session.add(Membership(id=uuid.uuid4(), user_id=user.id, org_id=org_id, role=role))
    await session.flush()


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


def header(user: User) -> dict:
    return {"Authorization": f"Bearer {user.keycloak_sub}"}


async def test_owner_invites_with_roles_on_several_tenants(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={
            "email": "priya@acme.test",
            "memberships": [
                {"org_id": str(two_tenants["org_a"]), "role": "recruiter"},
                {"org_id": str(two_tenants["org_b"]), "role": "viewer"},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "priya@acme.test"
    assert body["invitation_email_sent"] is True
    assert {m["role"] for m in body["memberships"]} == {"recruiter", "viewer"}
    assert keycloak["created"][0][0] == "priya@acme.test"


async def test_owner_cannot_grant_on_another_accounts_tenant(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={
            "email": "priya@acme.test",
            "memberships": [{"org_id": str(two_tenants["other_org"]), "role": "recruiter"}],
        },
    )

    assert response.status_code == 403
    assert not keycloak["created"]


async def test_a_tenant_admin_cannot_grant_on_an_org_they_do_not_administer(
    client, session, two_tenants, keycloak
):
    admin = await make_user(session, account_id=two_tenants["account"])
    await grant(session, admin, two_tenants["org_a"], "admin")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(admin),
        json={
            "email": "priya@acme.test",
            "memberships": [{"org_id": str(two_tenants["org_b"]), "role": "viewer"}],
        },
    )

    assert response.status_code == 403


async def test_a_tenant_admin_may_grant_on_their_own_org(client, session, two_tenants, keycloak):
    admin = await make_user(session, account_id=two_tenants["account"])
    await grant(session, admin, two_tenants["org_a"], "admin")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(admin),
        json={
            "email": "priya@acme.test",
            "memberships": [{"org_id": str(two_tenants["org_a"]), "role": "viewer"}],
        },
    )

    assert response.status_code == 200


@pytest.mark.parametrize("role", ["recruiter", "viewer"])
async def test_a_recruiter_or_viewer_is_denied_every_account_route(
    client, session, two_tenants, keycloak, role
):
    member = await make_user(session, account_id=two_tenants["account"])
    await grant(session, member, two_tenants["org_a"], role)
    account = two_tenants["account"]

    invite = await client.post(
        f"/accounts/{account}/invitations",
        headers=header(member),
        json={"email": "priya@acme.test"},
    )
    members = await client.get(f"/accounts/{account}/members", headers=header(member))
    tenants = await client.post(
        f"/accounts/{account}/tenants", headers=header(member), json={"name": "New Tenant"}
    )

    assert invite.status_code == 404
    assert members.status_code == 404
    assert tenants.status_code == 404


async def test_a_tenant_admin_cannot_modify_the_account_owner(
    client, session, two_tenants, keycloak
):
    admin = await make_user(session, account_id=two_tenants["account"])
    await grant(session, admin, two_tenants["org_a"], "admin")
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    await grant(session, owner, two_tenants["org_a"], "admin")

    response = await client.patch(
        f"/accounts/{two_tenants['account']}/members/{owner.id}",
        headers=header(admin),
        json={"memberships": []},
    )

    assert response.status_code == 403
    assert await session.scalar(
        select(Membership).where(Membership.user_id == owner.id)
    ) is not None


async def test_an_account_role_in_a_membership_is_rejected(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={
            "email": "priya@acme.test",
            "memberships": [{"org_id": str(two_tenants["org_a"]), "role": "account:owner"}],
        },
    )

    assert response.status_code == 422
    assert not keycloak["created"]


async def test_is_platform_admin_cannot_be_set_through_an_invitation(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={"email": "priya@acme.test", "is_platform_admin": True},
    )

    assert response.status_code == 200
    invited = await session.scalar(select(User).where(User.email == "priya@acme.test"))
    assert invited is not None
    assert invited.is_platform_admin is False


async def test_a_tenant_admin_cannot_create_an_owner(client, session, two_tenants, keycloak):
    admin = await make_user(session, account_id=two_tenants["account"])
    await grant(session, admin, two_tenants["org_a"], "admin")

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(admin),
        json={"email": "priya@acme.test", "account_role": "owner"},
    )

    assert response.status_code == 403


async def test_inviting_an_existing_address_returns_the_same_shape(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    payload = {
        "email": "priya@acme.test",
        "memberships": [{"org_id": str(two_tenants["org_a"]), "role": "viewer"}],
    }

    first = await client.post(
        f"/accounts/{two_tenants['account']}/invitations", headers=header(owner), json=payload
    )
    keycloak["existing"] = {"id": keycloak["created"][0][1], "emailVerified": False}
    second = await client.post(
        f"/accounts/{two_tenants['account']}/invitations", headers=header(owner), json=payload
    )

    assert first.status_code == second.status_code == 200
    assert first.json().keys() == second.json().keys()
    assert first.json()["user_id"] == second.json()["user_id"]


async def test_an_unverified_pre_existing_identity_is_refused(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    keycloak["existing"] = {"id": str(uuid.uuid4()), "emailVerified": False}

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={"email": "priya@acme.test"},
    )

    assert response.status_code == 409


async def test_an_address_on_another_account_is_refused_without_naming_it(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    outsider = await make_user(session, account_id=two_tenants["other_account"])

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={"email": outsider.email},
    )

    assert response.status_code == 409
    assert str(two_tenants["other_account"]) not in response.text
    assert not keycloak["created"]


async def test_a_keycloak_failure_leaves_no_local_state(client, session, two_tenants, keycloak):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    keycloak["fail"] = True

    response = await client.post(
        f"/accounts/{two_tenants['account']}/invitations",
        headers=header(owner),
        json={
            "email": "priya@acme.test",
            "memberships": [{"org_id": str(two_tenants["org_a"]), "role": "viewer"}],
        },
    )

    assert response.status_code == 502
    assert await session.scalar(select(User).where(User.email == "priya@acme.test")) is None


async def test_members_lists_the_account_and_their_memberships(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    member = await make_user(session, account_id=two_tenants["account"])
    await grant(session, member, two_tenants["org_a"], "recruiter")
    await make_user(session, account_id=two_tenants["other_account"])

    response = await client.get(
        f"/accounts/{two_tenants['account']}/members", headers=header(owner)
    )

    assert response.status_code == 200
    emails = {row["email"] for row in response.json()}
    assert emails == {owner.email, member.email}


async def test_patch_reconciles_the_membership_set(client, session, two_tenants, keycloak):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    member = await make_user(session, account_id=two_tenants["account"])
    await grant(session, member, two_tenants["org_a"], "recruiter")

    response = await client.patch(
        f"/accounts/{two_tenants['account']}/members/{member.id}",
        headers=header(owner),
        json={"memberships": [{"org_id": str(two_tenants["org_b"]), "role": "viewer"}]},
    )

    assert response.status_code == 200
    assert response.json()["memberships"] == [
        {"org_id": str(two_tenants["org_b"]), "role": "viewer"}
    ]


async def test_you_cannot_demote_your_own_admin_membership(client, session, two_tenants, keycloak):
    admin = await make_user(session, account_id=two_tenants["account"])
    await grant(session, admin, two_tenants["org_a"], "admin")

    response = await client.patch(
        f"/accounts/{two_tenants['account']}/members/{admin.id}",
        headers=header(admin),
        json={"memberships": [{"org_id": str(two_tenants["org_a"]), "role": "viewer"}]},
    )

    assert response.status_code == 403


async def test_delete_revokes_every_membership_in_the_account(
    client, session, two_tenants, keycloak
):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    member = await make_user(session, account_id=two_tenants["account"])
    await grant(session, member, two_tenants["org_a"], "recruiter")
    await grant(session, member, two_tenants["org_b"], "viewer")

    response = await client.delete(
        f"/accounts/{two_tenants['account']}/members/{member.id}", headers=header(owner)
    )

    assert response.status_code == 200
    remaining = (
        await session.scalars(select(Membership).where(Membership.user_id == member.id))
    ).all()
    assert remaining == []


async def test_only_an_owner_may_create_a_tenant(client, session, two_tenants, keycloak):
    admin = await make_user(session, account_id=two_tenants["account"])
    await grant(session, admin, two_tenants["org_a"], "admin")
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")

    denied = await client.post(
        f"/accounts/{two_tenants['account']}/tenants",
        headers=header(admin),
        json={"name": "Refused Tenant"},
    )
    allowed = await client.post(
        f"/accounts/{two_tenants['account']}/tenants",
        headers=header(owner),
        json={"name": f"New Tenant {uuid.uuid4()}"},
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200


async def test_account_role_does_not_widen_a_tenant_role(session, two_tenants):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    await grant(session, owner, two_tenants["org_a"], "viewer")

    dependency = require(Permission.USER_INVITE)
    with pytest.raises(Exception) as excinfo:
        await dependency(
            context=await _context(session, owner, two_tenants["org_a"]), session=session
        )

    assert getattr(excinfo.value, "status_code", None) == 403


async def _context(session: AsyncSession, user: User, org_id):
    from services.api.authz import tenant_context

    return await tenant_context(org_id=org_id, user=user, session=session)
