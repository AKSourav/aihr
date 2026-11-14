import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import AuditEvent, BreakGlassScope, Membership, User
from core.permissions import Permission, account_grants, grants
from services.api.auth import current_user

logger = logging.getLogger(__name__)


@dataclass
class TenantContext:
    org_id: UUID
    user: User
    role: str | None
    via_break_glass: bool


async def active_break_glass(
    session: AsyncSession, user_id: UUID, org_id: UUID
) -> BreakGlassScope | None:
    return await session.scalar(
        select(BreakGlassScope)
        .where(
            BreakGlassScope.user_id == user_id,
            BreakGlassScope.org_id == org_id,
            BreakGlassScope.expires_at > datetime.now(timezone.utc),
        )
        .order_by(BreakGlassScope.expires_at.desc())
    )


async def tenant_context(
    org_id: UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TenantContext:
    membership = await session.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.org_id == org_id)
    )
    if membership is not None:
        return TenantContext(org_id=org_id, user=user, role=membership.role, via_break_glass=False)

    if user.is_platform_admin and await active_break_glass(session, user.id, org_id) is not None:
        return TenantContext(org_id=org_id, user=user, role=None, via_break_glass=True)

    raise HTTPException(status_code=404, detail="not found")


def require(
    permission: Permission,
) -> Callable[..., Coroutine[Any, Any, TenantContext]]:
    async def dependency(
        context: TenantContext = Depends(tenant_context),
        session: AsyncSession = Depends(get_session),
    ) -> TenantContext:
        if context.via_break_glass:
            session.add(
                AuditEvent(
                    org_id=context.org_id,
                    actor_user_id=context.user.id,
                    entity_type="organization",
                    entity_id=context.org_id,
                    event_type="break_glass_access",
                    payload={"permission": str(permission)},
                )
            )
            await session.commit()
            logger.warning(
                "break glass access",
                extra={
                    "org_id": str(context.org_id),
                    "user_id": str(context.user.id),
                    "permission": str(permission),
                },
            )
            return context

        if context.role is not None and grants(context.role, permission):
            return context

        if account_grants(context.user.account_role, permission):
            return context

        raise HTTPException(status_code=403, detail="forbidden")

    return dependency


async def platform_admin(user: User = Depends(current_user)) -> User:
    if not user.is_platform_admin:
        raise HTTPException(status_code=404, detail="not found")
    return user
