from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, Organization, User
from core.permissions import ADMIN, OWNER
from services.api.auth import current_user


@dataclass
class AccountContext:
    account_id: UUID
    user: User
    is_owner: bool


async def owns_account(session: AsyncSession, user_id: UUID, account_id: UUID) -> bool:
    found = await session.scalar(
        select(Membership.id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(
            Membership.user_id == user_id,
            Membership.role == OWNER,
            Organization.account_id == account_id,
        )
        .limit(1)
    )
    return found is not None


async def account_context(
    account_id: UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AccountContext:
    if await owns_account(session, user.id, account_id):
        return AccountContext(account_id=account_id, user=user, is_owner=True)

    administers = await session.scalar(
        select(Membership.id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(
            Membership.user_id == user.id,
            Membership.role == ADMIN,
            Organization.account_id == account_id,
        )
        .limit(1)
    )
    if administers is not None:
        return AccountContext(account_id=account_id, user=user, is_owner=False)

    raise HTTPException(status_code=404, detail="not found")


async def owner_only(context: AccountContext = Depends(account_context)) -> AccountContext:
    if not context.is_owner:
        raise HTTPException(status_code=403, detail="forbidden")
    return context


async def authorize_grants(
    session: AsyncSession,
    context: AccountContext,
    org_ids: Mapping[UUID, str] | set[UUID],
    target: User | None = None,
) -> None:
    requested = set(org_ids)

    if requested:
        owned = set(
            (
                await session.scalars(
                    select(Organization.id)
                    .where(
                        Organization.account_id == context.account_id,
                        Organization.id.in_(requested),
                    )
                    .with_for_update(read=True)
                )
            ).all()
        )
        if requested - owned:
            raise HTTPException(status_code=403, detail="forbidden")

    held = {
        row.org_id: row.role
        for row in (
            await session.execute(
                select(Membership.org_id, Membership.role)
                .join(Organization, Organization.id == Membership.org_id)
                .where(
                    Membership.user_id == context.user.id,
                    Organization.account_id == context.account_id,
                )
                .with_for_update(read=True, of=Membership)
            )
        ).all()
    }

    is_owner = OWNER in held.values()
    if not is_owner:
        administered = {org_id for org_id, role in held.items() if role == ADMIN}
        if not administered or requested - administered:
            raise HTTPException(status_code=403, detail="forbidden")

    if target is not None and not is_owner:
        if await owns_account(session, target.id, context.account_id):
            raise HTTPException(status_code=403, detail="forbidden")

    context.is_owner = is_owner
