from temporalio.client import Client

from core.settings import get_settings

TASK_QUEUE = "voice"


async def temporal_client() -> Client:
    settings = get_settings()
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


def screening_workflow_id(job_id: object, candidate_id: object) -> str:
    return f"call-{job_id}-{candidate_id}"
