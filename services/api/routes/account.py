from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, Organization, User
from core.permissions import ADMIN, OWNER
from services.api import keycloak_admin
from services.api.account_authz import AccountContext, account_context, authorize_grants, owner_only

router = APIRouter(prefix="/accounts/{account_id}", tags=["accounts"])

TenantRole = Literal["owner", "admin", "recruiter", "viewer"]


class MembershipGrant(BaseModel):
    org_id: UUID
    role: TenantRole


class InvitationRequest(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    memberships: list[MembershipGrant] = Field(default_factory=list)


class MembershipOut(BaseModel):
    org_id: UUID
    role: str


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    memberships: list[MembershipOut]


class InvitationResponse(MemberOut):
    invitation_email_sent: bool


class MemberUpdate(BaseModel):
    memberships: list[MembershipGrant]


class TenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantResponse(BaseModel):
    org_id: UUID
    name: str


def requested_roles(memberships: list[MembershipGrant]) -> dict[UUID, str]:
    return {grant.org_id: grant.role for grant in memberships}


async def member_out(session: AsyncSession, user: User) -> MemberOut:
    rows = (
        await session.scalars(
            select(Membership)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user.id)
        )
    ).all()
    return MemberOut(
        user_id=user.id,
        email=user.email,
        memberships=[MembershipOut(org_id=row.org_id, role=row.role) for row in rows],
    )


async def memberships_in_account(
    session: AsyncSession, context: AccountContext, user_id: UUID
) -> dict[UUID, str]:
    rows = (
        await session.execute(
            select(Membership.org_id, Membership.role)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user_id, Organization.account_id == context.account_id)
        )
    ).all()
    return {row.org_id: row.role for row in rows}


async def reconcile(session: AsyncSession, user: User, desired: dict[UUID, str]) -> None:
    existing = {
        row.org_id: row
        for row in (
            await session.scalars(select(Membership).where(Membership.user_id == user.id))
        ).all()
    }
    for org_id, role in desired.items():
        row = existing.get(org_id)
        if row is None:
            session.add(Membership(user_id=user.id, org_id=org_id, role=role))
        else:
            row.role = role


@router.post("/invitations", response_model=InvitationResponse)
async def invite(
    body: InvitationRequest,
    context: AccountContext = Depends(account_context),
    session: AsyncSession = Depends(get_session),
) -> InvitationResponse:
    grants = requested_roles(body.memberships)
    await authorize_grants(session, context, grants)

    if OWNER in grants.values() and not context.is_owner:
        raise HTTPException(status_code=403, detail="forbidden")

    email = body.email.lower()
    email_sent = False
    user = await session.scalar(select(User).where(User.email == email))

    try:
        async with httpx.AsyncClient(timeout=keycloak_admin.TIMEOUT) as client:
            identity = await keycloak_admin.find_user_by_email(client, email)
            if identity is None:
                subject = await keycloak_admin.create_user(client, email)
            else:
                subject = identity["id"]
                known = await session.scalar(select(User).where(User.keycloak_sub == subject))
                if known is None and not identity.get("emailVerified", False):
                    raise HTTPException(status_code=409, detail="that address is already in use")
            email_sent = await keycloak_admin.send_invitation_email(client, subject)
    except keycloak_admin.KeycloakError as exc:
        raise HTTPException(status_code=502, detail="identity provider unavailable") from exc

    if user is None:
        user = await session.scalar(select(User).where(User.keycloak_sub == subject))
    if user is None:
        user = User(keycloak_sub=subject, email=email)
        session.add(user)
    await session.flush()

    await reconcile(session, user, grants)
    await session.commit()
    await session.refresh(user)

    member = await member_out(session, user)
    return InvitationResponse(**member.model_dump(), invitation_email_sent=email_sent)


@router.get("/members", response_model=list[MemberOut])
async def list_members(
    context: AccountContext = Depends(account_context),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    users = (
        await session.scalars(
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Organization.account_id == context.account_id)
            .order_by(User.email)
            .distinct()
        )
    ).all()
    return [await member_out(session, user) for user in users]


async def member_of_account(session: AsyncSession, context: AccountContext, user_id: UUID) -> User:
    user = await session.scalar(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(User.id == user_id, Organization.account_id == context.account_id)
        .limit(1)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="not found")
    return user


@router.patch("/members/{user_id}", response_model=MemberOut)
async def replace_memberships(
    user_id: UUID,
    body: MemberUpdate,
    context: AccountContext = Depends(account_context),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    target = await member_of_account(session, context, user_id)
    desired = requested_roles(body.memberships)

    current = await memberships_in_account(session, context, target.id)
    touched = set(desired) | {
        org_id for org_id, role in current.items() if desired.get(org_id) != role
    }
    await authorize_grants(session, context, touched, target)

    if OWNER in desired.values() and not context.is_owner:
        raise HTTPException(status_code=403, detail="forbidden")

    if target.id == context.user.id:
        losing_admin = [
            org_id
            for org_id, role in current.items()
            if role in (OWNER, ADMIN) and desired.get(org_id) != role
        ]
        if losing_admin:
            raise HTTPException(status_code=403, detail="cannot remove your own admin role")

    for org_id in current:
        if org_id not in desired and org_id in touched:
            await session.execute(
                delete(Membership).where(
                    Membership.user_id == target.id, Membership.org_id == org_id
                )
            )
    await reconcile(session, target, desired)
    await session.commit()

    return await member_out(session, target)


@router.delete("/members/{user_id}", response_model=MemberOut)
async def revoke_memberships(
    user_id: UUID,
    context: AccountContext = Depends(account_context),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    target = await member_of_account(session, context, user_id)
    current = set(await memberships_in_account(session, context, target.id))
    await authorize_grants(session, context, current, target)

    if target.id == context.user.id:
        raise HTTPException(status_code=403, detail="cannot revoke your own access")

    await session.execute(
        delete(Membership).where(
            Membership.user_id == target.id, Membership.org_id.in_(current)
        )
    )
    await session.commit()

    return await member_out(session, target)


@router.post("/tenants", response_model=TenantResponse)
async def create_tenant(
    body: TenantRequest,
    context: AccountContext = Depends(owner_only),
    session: AsyncSession = Depends(get_session),
) -> TenantResponse:
    await authorize_grants(session, context, set())
    if not context.is_owner:
        raise HTTPException(status_code=403, detail="forbidden")

    organization = Organization(account_id=context.account_id, name=body.name)
    session.add(organization)
    await session.commit()
    await session.refresh(organization)

    return TenantResponse(org_id=organization.id, name=organization.name)
