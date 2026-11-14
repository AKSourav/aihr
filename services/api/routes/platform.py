from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import AuditEvent, BreakGlassScope, Organization, User
from services.api.authz import platform_admin

router = APIRouter(prefix="/platform", tags=["platform"])

MAX_MINUTES = 240


class BreakGlassRequest(BaseModel):
    org_id: UUID
    reason: str = Field(min_length=10, max_length=500)
    minutes: int = Field(default=60, ge=5, le=MAX_MINUTES)


class BreakGlassResponse(BaseModel):
    org_id: UUID
    expires_at: datetime


@router.post("/break-glass", response_model=BreakGlassResponse)
async def open_break_glass(
    body: BreakGlassRequest,
    user: User = Depends(platform_admin),
    session: AsyncSession = Depends(get_session),
) -> BreakGlassResponse:
    organization = await session.get(Organization, body.org_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="not found")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=body.minutes)
    session.add(
        BreakGlassScope(
            user_id=user.id, org_id=body.org_id, reason=body.reason, expires_at=expires_at
        )
    )
    session.add(
        AuditEvent(
            org_id=body.org_id,
            actor_user_id=user.id,
            entity_type="organization",
            entity_id=body.org_id,
            event_type="break_glass_opened",
            payload={"reason": body.reason, "minutes": body.minutes},
        )
    )
    await session.commit()

    return BreakGlassResponse(org_id=body.org_id, expires_at=expires_at)
