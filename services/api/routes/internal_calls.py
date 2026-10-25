import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from core.db import get_session
from core.models import AuditEvent, Call, CandidateProfile, Organization
from core.schemas import CallContext, CallMode, CallOutcome
from core.settings import get_settings
from core.storage import Storage

logger = logging.getLogger(__name__)

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


@router.post("/{call_id}/complete", dependencies=[Depends(require_internal_token)])
async def complete_call(
    call_id: UUID, outcome: CallOutcome, session: AsyncSession = Depends(get_session)
) -> dict:
    if outcome.call_id != call_id:
        raise HTTPException(status_code=400, detail="call_id in body does not match URL")

    call = await session.scalar(select(Call).where(Call.id == call_id))
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")

    transcript_data = [turn.model_dump(mode="json") for turn in outcome.transcript]
    transcript_url = Storage().upload_json(f"transcripts/{call_id}.json", transcript_data)

    call.status = outcome.status.value
    call.callback_at = outcome.callback_at
    call.duration_seconds = outcome.duration_seconds
    call.error_detail = outcome.error_detail
    call.transcript_url = transcript_url

    session.add(
        AuditEvent(
            org_id=call.application.candidate.org_id,
            entity_type="call",
            entity_id=call_id,
            event_type="call_completed",
            payload={
                "status": outcome.status.value,
                "answered_question_ids": outcome.answered_question_ids,
            },
        )
    )
    await session.commit()

    settings = get_settings()
    workflow_id = f"call-{call.application.job_id}-{call.application.candidate_id}"
    try:
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("call_completed", outcome.model_dump(mode="json"))
    except Exception:
        logger.warning(
            "could not signal workflow (call may not have been started via a workflow)",
            extra={"call_id": str(call_id), "workflow_id": workflow_id},
            exc_info=True,
        )

    return {"status": "ok"}
