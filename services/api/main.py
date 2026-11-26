from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.logging import configure_logging
from core.resumes import MAX_UPLOAD_BYTES
from core.settings import get_settings
from services.api import mcp_server
from services.api.routes import (
    account,
    assistant,
    candidates,
    dev,
    internal_calls,
    jobs,
    mcp,
    me,
    opa,
    organizations,
    platform,
    provider_config,
    public_invites,
    resumes,
    screenings,
    signup,
    tenant,
    test_calls,
)

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The MCP session manager owns a task group; it must be running before any request
    # reaches POST /tenants/{org_id}/mcp.
    async with mcp_server.running_manager():
        yield


app = FastAPI(title="aihr-api", lifespan=lifespan)


@app.middleware("http")
async def limit_upload_size(request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_UPLOAD_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    return await call_next(request)

app.include_router(account.router)
app.include_router(internal_calls.router)
app.include_router(opa.router)
app.include_router(platform.router)
app.include_router(provider_config.router)
app.include_router(provider_config.catalogue_router)
app.include_router(tenant.router)
app.include_router(jobs.router)
app.include_router(candidates.router)
app.include_router(resumes.router)
app.include_router(me.router)
app.include_router(organizations.router)
app.include_router(screenings.router)
app.include_router(test_calls.router)
app.include_router(assistant.router)
app.include_router(mcp.router)
app.include_router(public_invites.router)
app.include_router(signup.router)
if get_settings().dev_routes_enabled:
    app.include_router(dev.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
