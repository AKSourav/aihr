import hashlib
import io
import json
import tarfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Membership, Organization, User
from core.permissions import (
    ADMIN,
    OWNER,
    platform_route_table,
    role_permission_table,
    route_table,
    self_route_table,
)


async def bundle_data(session: AsyncSession) -> dict:
    users = (await session.scalars(select(User))).all()
    memberships = (await session.scalars(select(Membership))).all()
    organizations = (await session.scalars(select(Organization))).all()

    sub_by_user_id = {user.id: user.keycloak_sub for user in users}
    account_by_org_id = {org.id: org.account_id for org in organizations}

    grants: dict[str, dict[str, str]] = {}
    access: dict[str, set[str]] = {}

    for membership in memberships:
        subject = sub_by_user_id.get(membership.user_id)
        if subject is None:
            continue
        grants.setdefault(subject, {})[str(membership.org_id)] = membership.role

        account_id = account_by_org_id.get(membership.org_id)
        if membership.role in (OWNER, ADMIN) and account_id is not None:
            access.setdefault(subject, set()).add(str(account_id))

    return {
        "memberships": grants,
        "account_access": {subject: sorted(ids) for subject, ids in access.items()},
        "platform_admins": sorted(user.keycloak_sub for user in users if user.is_platform_admin),
        "role_permissions": role_permission_table(),
        "routes": route_table(),
        "platform_routes": platform_route_table(),
        "self_routes": self_route_table(),
    }


MANIFEST_ROOTS = [
    "memberships",
    "account_access",
    "platform_admins",
    "role_permissions",
    "routes",
    "platform_routes",
    "self_routes",
]


def revision(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def pack(data: dict) -> bytes:
    manifest = {"revision": revision(data), "roots": MANIFEST_ROOTS}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as tar:
        for name, body in ((".manifest", manifest), ("data.json", data)):
            payload = json.dumps(body, sort_keys=True).encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()
