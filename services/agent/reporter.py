import asyncio
import logging

import httpx

from core.schemas import CallOutcome
from core.settings import get_settings

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
INITIAL_RETRY_DELAY_S = 1.0


async def report_outcome(outcome: CallOutcome) -> None:
    settings = get_settings()
    url = f"{settings.api_internal_url}/internal/calls/{outcome.call_id}/complete"
    headers = {"Authorization": f"Bearer {settings.internal_api_token}"}
    payload = outcome.model_dump(mode="json")

    delay = INITIAL_RETRY_DELAY_S
    async with httpx.AsyncClient() as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(url, json=payload, headers=headers, timeout=10.0)
                response.raise_for_status()
                logger.info(
                    "call outcome reported", extra={"call_id": str(outcome.call_id), "attempt": attempt}
                )
                return
            except httpx.HTTPError as exc:
                logger.warning(
                    "failed to report call outcome, retrying",
                    extra={"call_id": str(outcome.call_id), "attempt": attempt, "error": str(exc)},
                )
                if attempt == MAX_ATTEMPTS:
                    logger.error("giving up reporting call outcome", extra={"call_id": str(outcome.call_id)})
                    return
                await asyncio.sleep(delay)
                delay *= 2
