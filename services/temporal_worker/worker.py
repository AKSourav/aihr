import asyncio
import logging

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from core.logging import configure_logging
from core.settings import get_settings

logger = logging.getLogger(__name__)


@activity.defn
async def ping() -> str:
    return "pong"


async def run_worker(task_queue: str, client: Client) -> None:
    worker = Worker(client, task_queue=task_queue, activities=[ping])
    logger.info("worker started", extra={"task_queue": task_queue})
    await worker.run()


async def main() -> None:
    configure_logging()
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await asyncio.gather(
        run_worker("voice", client),
        run_worker("ai", client),
    )


if __name__ == "__main__":
    asyncio.run(main())
