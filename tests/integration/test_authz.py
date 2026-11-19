import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import AuditEvent, BreakGlassScope, Membership, User
from core.permissions import Permission
from services.api.authz import require, tenant_context


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


async def context_for(session, user, org_id):
    return await tenant_context(org_id=org_id, user=user, session=session)


async def check(session, user, org_id, permission):
    context = await context_for(session, user, org_id)
    return await require(permission)(context=context, session=session)


async def test_a_member_gets_their_role(session: AsyncSession, two_tenants):
    user = await make_user(session)
    await grant(session, user, two_tenants["org_a"], "recruiter")

    context = await context_for(session, user, two_tenants["org_a"])

    assert context.role == "recruiter"
    assert context.via_break_glass is False


async def test_a_non_member_gets_404_not_403(session: AsyncSession, two_tenants):
    user = await make_user(session)
    await grant(session, user, two_tenants["org_a"], "recruiter")

    with pytest.raises(HTTPException) as excinfo:
        await context_for(session, user, two_tenants["org_b"])

    assert excinfo.value.status_code == 404


async def test_recruiter_may_create_a_call(session: AsyncSession, two_tenants):
    user = await make_user(session)
    await grant(session, user, two_tenants["org_a"], "recruiter")

    context = await check(session, user, two_tenants["org_a"], Permission.CALL_CREATE)

    assert context.org_id == two_tenants["org_a"]


async def test_viewer_may_not_create_a_call(session: AsyncSession, two_tenants):
    user = await make_user(session)
    await grant(session, user, two_tenants["org_a"], "viewer")

    with pytest.raises(HTTPException) as excinfo:
        await check(session, user, two_tenants["org_a"], Permission.CALL_CREATE)

    assert excinfo.value.status_code == 403


async def test_viewer_may_not_read_transcripts(session: AsyncSession, two_tenants):
    user = await make_user(session)
    await grant(session, user, two_tenants["org_a"], "viewer")

    with pytest.raises(HTTPException) as excinfo:
        await check(session, user, two_tenants["org_a"], Permission.TRANSCRIPT_READ)

    assert excinfo.value.status_code == 403


async def test_revoking_membership_takes_effect_immediately(session: AsyncSession, two_tenants):
    user = await make_user(session)
    await grant(session, user, two_tenants["org_a"], "recruiter")
    await check(session, user, two_tenants["org_a"], Permission.CALL_CREATE)

    membership = await session.scalar(select(Membership).where(Membership.user_id == user.id))
    await session.delete(membership)
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await check(session, user, two_tenants["org_a"], Permission.CALL_CREATE)

    assert excinfo.value.status_code == 404


async def test_platform_admin_without_break_glass_is_refused(session: AsyncSession, two_tenants):
    admin = await make_user(session, is_platform_admin=True)

    with pytest.raises(HTTPException) as excinfo:
        await context_for(session, admin, two_tenants["org_a"])

    assert excinfo.value.status_code == 404


async def test_break_glass_grants_access_and_is_audited(session: AsyncSession, two_tenants):
    admin = await make_user(session, is_platform_admin=True)
    session.add(
        BreakGlassScope(
            id=uuid.uuid4(),
            user_id=admin.id,
            org_id=two_tenants["org_a"],
            reason="debugging call timeout",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        )
    )
    await session.flush()

    context = await check(session, admin, two_tenants["org_a"], Permission.TRANSCRIPT_READ)

    assert context.via_break_glass is True
    events = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "break_glass_access",
                AuditEvent.actor_user_id == admin.id,
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].actor_user_id == admin.id
    assert events[0].payload["permission"] == "transcript:read"


async def test_expired_break_glass_does_not_grant_access(session: AsyncSession, two_tenants):
    admin = await make_user(session, is_platform_admin=True)
    session.add(
        BreakGlassScope(
            id=uuid.uuid4(),
            user_id=admin.id,
            org_id=two_tenants["org_a"],
            reason="stale scope",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await context_for(session, admin, two_tenants["org_a"])

    assert excinfo.value.status_code == 404


async def test_break_glass_is_scoped_to_one_tenant(session: AsyncSession, two_tenants):
    admin = await make_user(session, is_platform_admin=True)
    session.add(
        BreakGlassScope(
            id=uuid.uuid4(),
            user_id=admin.id,
            org_id=two_tenants["org_a"],
            reason="debugging one tenant only",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        )
    )
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await context_for(session, admin, two_tenants["org_b"])

    assert excinfo.value.status_code == 404


async def test_break_glass_for_one_admin_does_not_cover_another(session: AsyncSession, two_tenants):
    opener = await make_user(session, is_platform_admin=True)
    other = await make_user(session, is_platform_admin=True)
    session.add(
        BreakGlassScope(
            id=uuid.uuid4(),
            user_id=opener.id,
            org_id=two_tenants["org_a"],
            reason="only this operator",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
        )
    )
    await session.flush()

    with pytest.raises(HTTPException) as excinfo:
        await context_for(session, other, two_tenants["org_a"])

    assert excinfo.value.status_code == 404


async def test_account_owner_may_not_read_tenant_data_without_membership(
    session: AsyncSession, two_tenants
):
    owner = await make_user(session, account_role="owner")

    with pytest.raises(HTTPException) as excinfo:
        await context_for(session, owner, two_tenants["org_a"])

    assert excinfo.value.status_code == 404
