import io
import json
import tarfile
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Membership, User
from services.api.bundle import bundle_data, pack, revision


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


async def test_bundle_maps_subject_to_tenant_role(session: AsyncSession, two_tenants):
    user = await make_user(session)
    session.add(Membership(id=uuid.uuid4(), user_id=user.id, org_id=two_tenants["org_a"], role="recruiter"))
    await session.flush()

    data = await bundle_data(session)

    assert data["memberships"][user.keycloak_sub] == {str(two_tenants["org_a"]): "recruiter"}


async def test_bundle_lists_platform_admins(session: AsyncSession):
    admin = await make_user(session, is_platform_admin=True)
    plain = await make_user(session)

    data = await bundle_data(session)

    assert admin.keycloak_sub in data["platform_admins"]
    assert plain.keycloak_sub not in data["platform_admins"]


async def test_bundle_omits_users_without_memberships(session: AsyncSession):
    user = await make_user(session)

    data = await bundle_data(session)

    assert user.keycloak_sub not in data["memberships"]


async def test_account_owner_reaches_their_account(session: AsyncSession, two_tenants):
    owner = await make_user(session, account_id=two_tenants["account"], account_role="owner")

    data = await bundle_data(session)

    assert data["account_access"][owner.keycloak_sub] == [str(two_tenants["account"])]


async def test_tenant_admin_reaches_the_account_of_their_org(session: AsyncSession, two_tenants):
    admin = await make_user(session)
    session.add(
        Membership(id=uuid.uuid4(), user_id=admin.id, org_id=two_tenants["org_a"], role="admin")
    )
    await session.flush()

    data = await bundle_data(session)

    assert data["account_access"][admin.keycloak_sub] == [str(two_tenants["account"])]


async def test_a_recruiter_reaches_no_account(session: AsyncSession, two_tenants):
    recruiter = await make_user(session)
    session.add(
        Membership(
            id=uuid.uuid4(), user_id=recruiter.id, org_id=two_tenants["org_a"], role="recruiter"
        )
    )
    await session.flush()

    data = await bundle_data(session)

    assert recruiter.keycloak_sub not in data["account_access"]


async def test_account_access_lists_are_sorted(session: AsyncSession, two_tenants):
    admin = await make_user(session, account_id=two_tenants["account"], account_role="owner")
    session.add(
        Membership(id=uuid.uuid4(), user_id=admin.id, org_id=two_tenants["other_org"], role="admin")
    )
    await session.flush()

    accounts = (await bundle_data(session))["account_access"][admin.keycloak_sub]

    assert len(accounts) == 2
    assert accounts == sorted(accounts)


async def test_bundle_carries_routes_and_role_permissions(session: AsyncSession):
    data = await bundle_data(session)

    assert data["routes"]
    assert all(entry["pattern"].startswith("^") for entry in data["routes"])
    assert "recruiter" in data["role_permissions"]
    assert "call:create" in data["role_permissions"]["recruiter"]


async def test_pack_produces_an_opa_bundle(session: AsyncSession):
    data = await bundle_data(session)

    archive = pack(data)

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == [".manifest", "data.json"]
        data_member = tar.extractfile("data.json")
        manifest_member = tar.extractfile(".manifest")
        assert data_member is not None and manifest_member is not None
        extracted = json.loads(data_member.read())
        manifest = json.loads(manifest_member.read())

    assert extracted == data
    assert manifest["revision"] == revision(data)
    assert "memberships" in manifest["roots"]
    assert "aihr" not in manifest["roots"]


async def test_pack_is_deterministic(session: AsyncSession):
    data = await bundle_data(session)

    assert pack(data) == pack(data)
    assert revision(data) == revision(data)


async def test_revision_changes_when_grants_change(session: AsyncSession, two_tenants):
    before = revision(await bundle_data(session))

    user = await make_user(session)
    session.add(Membership(id=uuid.uuid4(), user_id=user.id, org_id=two_tenants["org_a"], role="viewer"))
    await session.flush()

    assert revision(await bundle_data(session)) != before
