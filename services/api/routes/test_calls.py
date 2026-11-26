"""A self-test call: the signed-in recruiter joins a real screening call as the candidate.

It goes through the same invite link, room dispatch and agent the real candidate flow uses, so
it genuinely exercises STT, TTS and the conversational model. It deliberately does not start a
Temporal workflow — there is nothing to chase, retry or summarise for a test.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.invites import invite_url, issue_invite_token
from core.models import Application, AuditEvent, Call, Candidate, Job
from core.permissions import Permission
from core.schemas import CallMode
from core.settings import get_settings
from services.api.authz import TenantContext, require

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{org_id}", tags=["test-calls"])

TEST_CALL_TTL = timedelta(hours=1)
JOINABLE = "invited"


class TestCallRequest(BaseModel):
    job_id: UUID


class TestCallOut(BaseModel):
    call_id: UUID
    application_id: UUID
    join_url: str
    expires_at: datetime


async def _self_test_candidate(
    session: AsyncSession, context: TenantContext
) -> Candidate:
    email = f"selftest-{context.user.id}@candidates.test"
    candidate = await session.scalar(
        select(Candidate).where(
            Candidate.org_id == context.org_id, Candidate.email == email
        )
    )
    if candidate is not None:
        return candidate

    candidate = Candidate(
        org_id=context.org_id,
        first_name=context.user.email.split("@")[0][:100] or "Tester",
        last_name="(self test)",
        email=email,
        consent_ai_call_at=datetime.now(timezone.utc),
    )
    session.add(candidate)
    await session.flush()
    return candidate


@router.post("/test-calls", response_model=TestCallOut, status_code=201)
async def start_test_call(
    body: TestCallRequest,
    context: TenantContext = Depends(require(Permission.CALL_CREATE)),
    session: AsyncSession = Depends(get_session),
) -> TestCallOut:
    settings = get_settings()
    if len(settings.invite_signing_key) < 32:
        raise HTTPException(
            status_code=503, detail="call invites are not configured on this server"
        )
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        raise HTTPException(
            status_code=503, detail="the voice service is not configured on this server"
        )

    job = await session.scalar(
        select(Job).where(Job.id == body.job_id, Job.org_id == context.org_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="not found")

    candidate = await _self_test_candidate(session, context)

    application = await session.scalar(
        select(Application).where(
            Application.org_id == context.org_id,
            Application.candidate_id == candidate.id,
            Application.job_id == job.id,
        )
    )
    if application is None:
        application = Application(
            org_id=context.org_id,
            candidate_id=candidate.id,
            job_id=job.id,
            stage="self_test",
        )
        session.add(application)
        await session.flush()

    open_calls = (
        await session.scalars(
            select(Call).where(
                Call.org_id == context.org_id,
                Call.application_id == application.id,
                Call.status == JOINABLE,
            )
        )
    ).all()
    for stale in open_calls:
        stale.status = "expired"

    now = datetime.now(timezone.utc)
    expires_at = now + TEST_CALL_TTL
    call = Call(
        org_id=context.org_id,
        application_id=application.id,
        attempt_no=len(open_calls) + 1,
        call_mode=CallMode.web.value,
        status=JOINABLE,
        invite_sent_at=now,
        invite_expires_at=expires_at,
    )
    session.add(call)
    await session.flush()

    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="call",
            entity_id=call.id,
            event_type="test_call_started",
            payload={"job_id": str(job.id)},
        )
    )
    await session.commit()

    logger.info(
        "test call created", extra={"call_id": str(call.id), "job_id": str(job.id)}
    )

    return TestCallOut(
        call_id=call.id,
        application_id=application.id,
        join_url=invite_url(issue_invite_token(call.id, expires_at)),
        expires_at=expires_at,
    )
