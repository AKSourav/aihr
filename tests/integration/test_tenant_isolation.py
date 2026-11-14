import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Application, Call, Candidate


async def test_application_cannot_span_tenants_claiming_org_a(session: AsyncSession, two_tenants):
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                Application(
                    id=uuid.uuid4(),
                    org_id=two_tenants["org_a"],
                    candidate_id=two_tenants["candidate_a"],
                    job_id=two_tenants["job_b"],
                    stage="shortlisted",
                )
            )
            await session.flush()


async def test_application_cannot_span_tenants_claiming_org_b(session: AsyncSession, two_tenants):
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                Application(
                    id=uuid.uuid4(),
                    org_id=two_tenants["org_b"],
                    candidate_id=two_tenants["candidate_a"],
                    job_id=two_tenants["job_b"],
                    stage="shortlisted",
                )
            )
            await session.flush()


async def test_same_tenant_application_is_allowed(session: AsyncSession, two_tenants):
    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()

    assert application.org_id == two_tenants["org_a"]


async def test_call_cannot_claim_another_tenant(session: AsyncSession, two_tenants):
    application = Application(
        id=uuid.uuid4(),
        org_id=two_tenants["org_a"],
        candidate_id=two_tenants["candidate_a"],
        job_id=two_tenants["job_a"],
        stage="shortlisted",
    )
    session.add(application)
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                Call(
                    id=uuid.uuid4(),
                    org_id=two_tenants["org_b"],
                    application_id=application.id,
                    call_mode="web",
                    status="dispatching",
                )
            )
            await session.flush()


async def test_candidate_email_is_unique_per_tenant_not_globally(session: AsyncSession, two_tenants):
    twin = Candidate(
        id=uuid.uuid4(),
        org_id=two_tenants["org_b"],
        first_name="Asha",
        last_name="Rao",
        email="asha@candidates.test",
    )
    session.add(twin)
    await session.flush()

    assert twin.org_id == two_tenants["org_b"]
