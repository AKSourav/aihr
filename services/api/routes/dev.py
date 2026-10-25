from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Application, Candidate, Call
from core.schemas import CallMode

router = APIRouter(prefix="/dev/calls", tags=["dev"])


class CreateDevCallRequest(BaseModel):
    candidate_key: str
    call_mode: CallMode = CallMode.console


class CreateDevCallResponse(BaseModel):
    call_id: UUID
    application_id: UUID
    candidate_email: str
    call_mode: CallMode


@router.post("", response_model=CreateDevCallResponse)
async def create_dev_call(
    body: CreateDevCallRequest, session: AsyncSession = Depends(get_session)
) -> CreateDevCallResponse:
    email = f"{body.candidate_key}@candidates.test"
    candidate = await session.scalar(select(Candidate).where(Candidate.email == email))
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no seeded candidate for key '{body.candidate_key}'")

    application = await session.scalar(
        select(Application).where(Application.candidate_id == candidate.id).order_by(Application.created_at)
    )
    if application is None:
        raise HTTPException(status_code=404, detail=f"candidate '{body.candidate_key}' has no application")

    call = Call(
        application_id=application.id,
        attempt_no=1,
        call_mode=body.call_mode.value,
        status="dispatching",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)

    return CreateDevCallResponse(
        call_id=call.id,
        application_id=application.id,
        candidate_email=candidate.email,
        call_mode=body.call_mode,
    )
