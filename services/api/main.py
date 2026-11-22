from fastapi import FastAPI

from core.logging import configure_logging
from core.settings import get_settings
from services.api.routes import (
    account,
    candidates,
    dev,
    internal_calls,
    jobs,
    me,
    opa,
    organizations,
    platform,
    public_invites,
    screenings,
    signup,
    tenant,
)

configure_logging()

app = FastAPI(title="aihr-api")

app.include_router(account.router)
app.include_router(internal_calls.router)
app.include_router(opa.router)
app.include_router(platform.router)
app.include_router(tenant.router)
app.include_router(jobs.router)
app.include_router(candidates.router)
app.include_router(me.router)
app.include_router(organizations.router)
app.include_router(screenings.router)
app.include_router(public_invites.router)
app.include_router(signup.router)
if get_settings().dev_routes_enabled:
    app.include_router(dev.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
