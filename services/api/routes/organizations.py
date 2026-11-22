from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Account, Membership, Organization, User
from core.permissions import OWNER
from services.api.auth import current_user

router = APIRouter(tags=["organizations"])


class OrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class OrganizationResponse(BaseModel):
    org_id: UUID
    name: str
    account_id: UUID
    role: str


async def found_organization(session: AsyncSession, user: User, name: str) -> Organization:
    account = Account(name=name)
    session.add(account)
    await session.flush()

    organization = Organization(account_id=account.id, name=name)
    session.add(organization)
    await session.flush()

    session.add(Membership(user_id=user.id, org_id=organization.id, role=OWNER))
    return organization


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
async def create_organization(
    body: OrganizationRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationResponse:
    organization = await found_organization(session, user, body.name)
    await session.commit()
    await session.refresh(organization)

    return OrganizationResponse(
        org_id=organization.id,
        name=organization.name,
        account_id=organization.account_id,
        role=OWNER,
    )
