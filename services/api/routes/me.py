from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, Organization, User
from core.permissions import ROLE_PERMISSIONS
from services.api.auth import current_user

router = APIRouter(tags=["me"])


class MembershipOut(BaseModel):
    org_id: UUID
    org_name: str
    role: str
    permissions: list[str]


class MeOut(BaseModel):
    user_id: UUID
    email: str
    is_platform_admin: bool
    memberships: list[MembershipOut]


@router.get("/me", response_model=MeOut)
async def me(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> MeOut:
    rows = (
        await session.execute(
            select(Membership, Organization)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user.id)
            .order_by(Organization.name)
        )
    ).all()

    return MeOut(
        user_id=user.id,
        email=user.email,
        is_platform_admin=user.is_platform_admin,
        memberships=[
            MembershipOut(
                org_id=org.id,
                org_name=org.name,
                role=membership.role,
                permissions=[str(p) for p in ROLE_PERMISSIONS.get(membership.role, [])],
            )
            for membership, org in rows
        ],
    )
