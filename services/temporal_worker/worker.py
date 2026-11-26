import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from core.logging import configure_logging
from core.settings import get_settings
from services.temporal_worker.activities.extract_resume import extract_resume, fail_resume
from services.temporal_worker.activities.mark_unreachable import mark_unreachable
from services.temporal_worker.activities.place_call import place_call
from services.temporal_worker.activities.score_resume import score_resume
from services.temporal_worker.activities.send_invite import expire_invite, send_invite
from services.temporal_worker.activities.summarize_call import summarize_call
from services.temporal_worker.workflows.extract_resume import ExtractResumeWorkflow
from services.temporal_worker.workflows.screening_call import ScreeningCallWorkflow

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue="voice",
        workflows=[ScreeningCallWorkflow, ExtractResumeWorkflow],
        activities=[
            place_call,
            send_invite,
            expire_invite,
            mark_unreachable,
            summarize_call,
            extract_resume,
            fail_resume,
            score_resume,
        ],
    )
    logger.info("worker started", extra={"task_queue": "voice"})
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
