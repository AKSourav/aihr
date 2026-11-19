import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.exceptions import WorkflowAlreadyStartedError

from core.db import get_session
from core.models import Application, AuditEvent
from core.permissions import Permission
from core.schemas import CallMode
from core.settings import get_settings
from services.api.authz import TenantContext, require
from services.api.temporal import TASK_QUEUE, screening_workflow_id, temporal_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{org_id}/applications", tags=["screenings"])


class StartScreeningRequest(BaseModel):
    channel: Literal["email", "phone"] = "email"


class StartScreeningResponse(BaseModel):
    workflow_id: str
    channel: str


@router.post("/{application_id}/screenings", response_model=StartScreeningResponse, status_code=202)
async def start_screening(
    application_id: UUID,
    body: StartScreeningRequest,
    context: TenantContext = Depends(require(Permission.CALL_CREATE)),
    session: AsyncSession = Depends(get_session),
) -> StartScreeningResponse:
    application = await session.scalar(
        select(Application).where(
            Application.id == application_id, Application.org_id == context.org_id
        )
    )
    if application is None:
        raise HTTPException(status_code=404, detail="not found")

    if body.channel == "phone":
        raise HTTPException(
            status_code=501, detail="phone screenings are not available yet; use channel 'email'"
        )

    settings = get_settings()
    if not settings.smtp_host or len(settings.invite_signing_key) < 32:
        raise HTTPException(status_code=503, detail="email invites are not configured on this server")

    workflow_id = screening_workflow_id(application.job_id, application.candidate_id)
    try:
        client = await temporal_client()
        # A plain dict serialises to the same JSON as ScreeningCallInput; the API image does not
        # ship the worker package, so it cannot import the dataclass.
        await client.start_workflow(
            "ScreeningCallWorkflow",
            {
                "application_id": str(application.id),
                "call_mode": CallMode.email.value,
                "signal_timeout_min": settings.call_signal_timeout_min,
                "invite_ttl_hours": settings.invite_ttl_hours,
            },
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError as exc:
        raise HTTPException(
            status_code=409, detail="a screening is already in progress for this application"
        ) from exc

    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="application",
            entity_id=application.id,
            event_type="screening_started",
            payload={"channel": body.channel, "workflow_id": workflow_id},
        )
    )
    await session.commit()
    logger.info("screening started", extra={"workflow_id": workflow_id, "channel": body.channel})

    return StartScreeningResponse(workflow_id=workflow_id, channel=body.channel)
