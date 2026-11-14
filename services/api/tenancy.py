from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Organization


async def tenant_id(org_id: UUID, session: AsyncSession = Depends(get_session)) -> UUID:
    organization = await session.get(Organization, org_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="not found")
    return org_id
