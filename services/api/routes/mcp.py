"""The MCP endpoint, mounted inside the tenant prefix.

It is a real FastAPI route rather than an app.mount() for two reasons: it must appear in
/openapi.json (tests/unit/test_api_access.py asserts every mapped route does), and its
Depends chain is how the caller's identity and session reach the tools at all.

Starlette hands a route's Response the original `receive`, so the JSON-RPC body is still
unconsumed here — which holds only as long as no dependency reads the request body.
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.permissions import Permission
from services.api.authz import TenantContext, require
from services.api.mcp_server import CALLER, Caller, current_manager

router = APIRouter(prefix="/tenants/{org_id}", tags=["mcp"])


class StreamableResponse(Response):
    def __init__(self, token) -> None:
        self._token = token
        super().__init__()

    async def __call__(self, scope, receive, send) -> None:
        try:
            await current_manager().handle_request(scope, receive, send)
        finally:
            CALLER.reset(self._token)


@router.post("/mcp", include_in_schema=True)
async def mcp_endpoint(
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    return StreamableResponse(CALLER.set(Caller(session=session, context=context)))
