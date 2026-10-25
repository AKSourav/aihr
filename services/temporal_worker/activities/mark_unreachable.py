import logging
from uuid import UUID

from temporalio import activity

from core.db import async_session
from core.models import Application, AuditEvent
from services.temporal_worker.shared import MarkUnreachableInput

logger = logging.getLogger(__name__)


@activity.defn
async def mark_unreachable(input: MarkUnreachableInput) -> None:
    application_id = UUID(input.application_id)

    async with async_session() as session:
        application = await session.get(Application, application_id)
        if application is None:
            return

        application.stage = "unreachable"
        session.add(
            AuditEvent(
                org_id=application.candidate.org_id,
                entity_type="application",
                entity_id=application_id,
                event_type="application_unreachable",
                payload={},
            )
        )
        await session.commit()

    logger.info("application marked unreachable", extra={"application_id": input.application_id})
