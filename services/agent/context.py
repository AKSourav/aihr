import httpx

from core.schemas import CallContext
from core.settings import get_settings


async def fetch_call_context(call_id: str) -> CallContext:
    settings = get_settings()
    url = f"{settings.api_internal_url}/internal/calls/{call_id}/context"
    headers = {"Authorization": f"Bearer {settings.internal_api_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        return CallContext.model_validate(response.json())
