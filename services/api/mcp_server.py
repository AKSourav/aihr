"""The MCP face of the assistant's tool registry.

Every tool call runs under the HTTP caller's own session and TenantContext, which the route
puts in a ContextVar before handing the request to the session manager. The manager is
constructed stateless and POST-only precisely so a tools/call can never arrive on a different
request than the one that set that ContextVar.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import mcp.types as types
from fastapi import HTTPException
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.ext.asyncio import AsyncSession

from core.assistant_tools import TOOLS
from services.api.authz import TenantContext, ensure

logger = logging.getLogger(__name__)

SERVER_NAME = "aihr"


@dataclass
class Caller:
    session: AsyncSession
    context: TenantContext


CALLER: ContextVar[Caller] = ContextVar("mcp_caller")


def _text(payload: dict) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, default=str))]
    )


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.args.model_json_schema(),
            )
            for tool in TOOLS.values()
        ]
    )


async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        caller = CALLER.get()
    except LookupError:
        logger.error("mcp tool call arrived with no caller in context")
        return _text({"error": "no_caller"})

    tool = TOOLS.get(params.name)
    if tool is None:
        return _text({"error": "unknown_tool", "tool": params.name})

    try:
        await ensure(caller.session, caller.context, tool.permission)
    except HTTPException:
        return _text({"error": "permission_denied", "permission": str(tool.permission)})

    try:
        args = tool.args.model_validate(params.arguments or {})
    except Exception as exc:
        return _text({"error": "invalid_arguments", "detail": str(exc)[:400]})

    try:
        result = await tool.run(
            caller.session, caller.context.org_id, caller.context.user.id, args
        )
    except Exception as exc:
        logger.exception("mcp tool failed", extra={"tool": params.name})
        return _text({"error": "tool_failed", "detail": str(exc)[:200]})

    return _text(result)


server: Server = Server(
    SERVER_NAME,
    version="1",
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)

# A session manager may only be run once, so one is built per application startup rather
# than at import — the test suite reloads the app repeatedly.
_manager: StreamableHTTPSessionManager | None = None


@asynccontextmanager
async def running_manager() -> AsyncIterator[None]:
    global _manager
    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
    _manager = manager
    try:
        async with manager.run():
            yield
    finally:
        _manager = None


def current_manager() -> StreamableHTTPSessionManager:
    if _manager is None:
        raise HTTPException(status_code=503, detail="the MCP server is not running")
    return _manager
