"""Candidate-facing endpoints for email invites.

These are unauthenticated: the signed invite token in the URL is the credential. A token only
ever unlocks the one call it was issued for, and only while that call is still joinable.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.invites import ExpiredInvite, InvalidInvite, is_expired, verify_invite_token
from core.livekit_rooms import candidate_join_token, create_room_and_dispatch
from core.models import AuditEvent, Call, Organization
from core.schemas import CallMode
from core.settings import get_settings
from services.api.temporal import screening_workflow_id, temporal_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public"])

JOIN_PAGE = Path(__file__).resolve().parent.parent / "static" / "join.html"

JOINABLE = "invited"
IN_PROGRESS = "in_progress"

InviteState = Literal["ready", "in_progress", "finished"]


class InviteOut(BaseModel):
    state: InviteState
    company_name: str
    job_title: str
    candidate_first_name: str
    expires_at: datetime | None
    max_minutes: int


class JoinOut(BaseModel):
    livekit_url: str
    token: str
    room_name: str


def _call_id_from(token: str) -> UUID:
    try:
        return verify_invite_token(token)
    except ExpiredInvite as exc:
        raise HTTPException(status_code=410, detail="this invite has expired") from exc
    except InvalidInvite as exc:
        raise HTTPException(status_code=404, detail="invite not found") from exc


def _state(call: Call) -> InviteState:
    if call.status == JOINABLE:
        return "ready"
    if call.status == IN_PROGRESS:
        return "in_progress"
    return "finished"


async def _load_call(session: AsyncSession, call_id: UUID, lock: bool = False) -> Call:
    query = select(Call).where(Call.id == call_id)
    if lock:
        query = query.with_for_update(of=Call)
    call = await session.scalar(query)
    if call is None or call.call_mode != CallMode.email.value:
        raise HTTPException(status_code=404, detail="invite not found")
    if call.status == "expired" or (call.status == JOINABLE and is_expired(call.invite_expires_at)):
        raise HTTPException(status_code=410, detail="this invite has expired")
    return call


@router.get("/join/{token}", response_class=HTMLResponse, include_in_schema=False)
async def join_page(token: str) -> HTMLResponse:
    return HTMLResponse(JOIN_PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/public/invites/{token}", response_model=InviteOut)
async def get_invite(token: str, session: AsyncSession = Depends(get_session)) -> InviteOut:
    call = await _load_call(session, _call_id_from(token))
    job = call.application.job
    organization = await session.get(Organization, call.org_id)

    return InviteOut(
        state=_state(call),
        company_name=organization.name if organization else "",
        job_title=job.title,
        candidate_first_name=call.application.candidate.first_name,
        expires_at=call.invite_expires_at,
        max_minutes=int(job.question_plan.get("max_minutes", get_settings().max_call_duration_min)),
    )


@router.post("/public/invites/{token}/join", response_model=JoinOut)
async def join_invite(token: str, session: AsyncSession = Depends(get_session)) -> JoinOut:
    call = await _load_call(session, _call_id_from(token), lock=True)
    state = _state(call)
    if state == "finished":
        raise HTTPException(status_code=409, detail="this screening call has already finished")

    first_join = state == "ready"
    if first_join:
        try:
            call.room_name = await create_room_and_dispatch(call.id)
        except Exception as exc:
            logger.exception("could not dispatch agent for invite", extra={"call_id": str(call.id)})
            raise HTTPException(status_code=502, detail="could not start the call, please try again") from exc
        call.status = IN_PROGRESS
        session.add(
            AuditEvent(
                org_id=call.org_id,
                entity_type="call",
                entity_id=call.id,
                event_type="invite_joined",
                payload={},
            )
        )

    room_name = str(call.room_name)
    application = call.application
    candidate_name = application.candidate.first_name
    job_id, candidate_id, call_id = application.job_id, application.candidate_id, call.id
    await session.commit()

    if first_join:
        workflow_id = screening_workflow_id(job_id, candidate_id)
        try:
            client = await temporal_client()
            await client.get_workflow_handle(workflow_id).signal("call_joined", str(call_id))
        except Exception:
            logger.warning(
                "could not signal call_joined",
                extra={"call_id": str(call_id), "workflow_id": workflow_id},
                exc_info=True,
            )

    return JoinOut(
        livekit_url=get_settings().livekit_url,
        token=candidate_join_token(room_name, candidate_name),
        room_name=room_name,
    )
