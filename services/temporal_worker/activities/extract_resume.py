import logging
from uuid import UUID

from temporalio import activity

from core.db import async_session
from core.resumes import fail_resume as fail_resume_row
from core.resumes import ingest_resume
from services.temporal_worker.shared import ExtractResumeInput, FailResumeInput

logger = logging.getLogger(__name__)


@activity.defn
async def extract_resume(input: ExtractResumeInput) -> str | None:
    async with async_session() as session:
        return await ingest_resume(session, UUID(input.resume_id))


@activity.defn
async def fail_resume(input: FailResumeInput) -> None:
    async with async_session() as session:
        await fail_resume_row(session, UUID(input.resume_id), input.reason)
    logger.warning("resume extraction gave up", extra={"resume_id": input.resume_id})
