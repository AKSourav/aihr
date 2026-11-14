import io
import json
import tarfile
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Membership, User
from services.api.bundle import bundle_data, pack, revision


async def make_user(session: AsyncSession, **kwargs) -> User:
    user = User(id=uuid.uuid4(), keycloak_sub=str(uuid.uuid4()), email="x@acme.test", **kwargs)
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
