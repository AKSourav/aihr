import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Membership, User
from services.api import auth


@pytest.fixture(autouse=True)
def stub_token(monkeypatch):
    def fake_decode(token: str) -> dict:
        return {"sub": token, "email": f"{token}@acme.test"}

    monkeypatch.setattr(auth, "decode_token", fake_decode)


async def test_first_login_provisions_a_user(session: AsyncSession):
    subject = str(uuid.uuid4())

    user = await auth.current_user(token=subject, session=session)

    assert user.keycloak_sub == subject
    assert user.email == f"{subject}@acme.test"


async def test_a_provisioned_user_has_no_tenant_access_by_default(session: AsyncSession):
    subject = str(uuid.uuid4())

    user = await auth.current_user(token=subject, session=session)

    memberships = (
        await session.scalars(select(Membership).where(Membership.user_id == user.id))
    ).all()
    assert memberships == []
    assert user.is_platform_admin is False


async def test_second_login_reuses_the_same_user(session: AsyncSession):
    subject = str(uuid.uuid4())

    first = await auth.current_user(token=subject, session=session)
    second = await auth.current_user(token=subject, session=session)

    assert first.id == second.id
    rows = (await session.scalars(select(User).where(User.keycloak_sub == subject))).all()
    assert len(rows) == 1
