import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from temporalio import activity

from core.db import async_session
from core.email import Invite, render_invite, send_email
from core.invites import invite_url, issue_invite_token
from core.models import Application, AuditEvent, Call, Organization
from core.schemas import CallMode
from core.settings import get_settings
from services.temporal_worker.shared import ExpireInviteInput, SendInviteInput

logger = logging.getLogger(__name__)


@activity.defn
async def send_invite(input: SendInviteInput) -> None:
    """Create the call row for this attempt and email the candidate a personal join link.

    No LiveKit room exists yet: the agent is dispatched only when the candidate opens the link.
    Delivery is at-least-once; a retry after a successful send but failed commit re-sends the email.
    """
    call_id = UUID(input.call_id)
    settings = get_settings()

    async with async_session() as session:
        application = await session.get(Application, UUID(input.application_id))
        if application is None:
            raise ValueError(f"application {input.application_id} not found")

        call = await session.get(Call, call_id)
        if call is None:
            call = Call(
                id=call_id,
                org_id=application.org_id,
                application_id=application.id,
                attempt_no=input.attempt_no,
                call_mode=CallMode.email.value,
                status="inviting",
            )
            session.add(call)
            await session.flush()

        if call.invite_sent_at is not None:
            return

        organization = await session.get(Organization, application.org_id)
        if organization is None:
            raise ValueError(f"organization {application.org_id} not found")

        candidate = application.candidate
        job = application.job
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=input.ttl_hours)
        token = issue_invite_token(call_id, expires_at)

        message = render_invite(
            Invite(
                to_email=candidate.email,
                candidate_first_name=candidate.first_name,
                company_name=organization.name,
                job_title=job.title,
                join_url=invite_url(token),
                expires_at=expires_at,
                max_minutes=int(job.question_plan.get("max_minutes", settings.max_call_duration_min)),
            )
        )
        await asyncio.to_thread(send_email, message)

        call.status = "invited"
        call.invite_sent_at = now
        call.invite_expires_at = expires_at
        session.add(
            AuditEvent(
                org_id=application.org_id,
                entity_type="call",
                entity_id=call_id,
                event_type="invite_sent",
                payload={"attempt_no": input.attempt_no, "expires_at": expires_at.isoformat()},
            )
        )
        await session.commit()

    logger.info("invite sent", extra={"call_id": input.call_id, "attempt_no": input.attempt_no})


@activity.defn
async def expire_invite(input: ExpireInviteInput) -> None:
    call_id = UUID(input.call_id)

    async with async_session() as session:
        call = await session.get(Call, call_id)
        if call is None or call.status != "invited":
            return

        call.status = "expired"
        session.add(
            AuditEvent(
                org_id=call.org_id,
                entity_type="call",
                entity_id=call_id,
                event_type="invite_expired",
                payload={},
            )
        )
        await session.commit()

    logger.info("invite expired", extra={"call_id": input.call_id})
