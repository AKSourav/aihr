import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from core.models import Account, Candidate, Job, Organization
from core.settings import get_settings


@pytest.fixture
async def session():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        async with AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as s:
            yield s
        await outer.rollback()
    await engine.dispose()


@pytest.fixture
async def two_tenants(session: AsyncSession):
    account = Account(id=uuid.uuid4(), name=f"acct-{uuid.uuid4()}")
    org_a = Organization(id=uuid.uuid4(), account_id=account.id, name=f"A-{uuid.uuid4()}")
    org_b = Organization(id=uuid.uuid4(), account_id=account.id, name=f"B-{uuid.uuid4()}")
    session.add_all([account, org_a, org_b])
    await session.flush()

    candidate_a = Candidate(
        id=uuid.uuid4(),
        org_id=org_a.id,
        first_name="Asha",
        last_name="Rao",
        email="asha@candidates.test",
    )
    job_a = Job(id=uuid.uuid4(), org_id=org_a.id, title="A job", rubric=[], question_plan={})
    job_b = Job(id=uuid.uuid4(), org_id=org_b.id, title="B job", rubric=[], question_plan={})
    session.add_all([candidate_a, job_a, job_b])
    await session.flush()

    return {
        "org_a": org_a.id,
        "org_b": org_b.id,
        "candidate_a": candidate_a.id,
        "job_a": job_a.id,
        "job_b": job_b.id,
    }
