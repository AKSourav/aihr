from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Call, CandidateProfile, Organization
from core.schemas import CallContext, CallMode
from core.settings import get_settings

router = APIRouter(prefix="/internal/calls", tags=["internal"])


def require_internal_token(authorization: str = Header(default="")) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.internal_api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


@router.get("/{call_id}/context", response_model=CallContext, dependencies=[Depends(require_internal_token)])
async def get_call_context(call_id: UUID, session: AsyncSession = Depends(get_session)) -> CallContext:
    call = await session.scalar(select(Call).where(Call.id == call_id))
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")

    application = call.application
    candidate = application.candidate
    job = application.job
    organization = await session.get(Organization, job.org_id)
    profile = await session.scalar(select(CandidateProfile).where(CandidateProfile.candidate_id == candidate.id))

    return CallContext(
        call_id=call.id,
        application_id=application.id,
        call_mode=CallMode(call.call_mode),
        phone_e164=candidate.phone_e164,
        company_name=organization.name,
        job_title=job.title,
        candidate_first_name=candidate.first_name,
        profile_summary=profile.data.get("summary", "") if profile else "",
        question_plan=job.question_plan,
        recording_consent_on_file=candidate.consent_recording_at is not None,
        max_minutes=job.question_plan.get("max_minutes", 15),
    )
