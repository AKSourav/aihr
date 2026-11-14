from fastapi import FastAPI

from core.logging import configure_logging
from core.settings import get_settings
from services.api.routes import dev, internal_calls, opa

configure_logging()

app = FastAPI(title="aihr-api")

app.include_router(internal_calls.router)
app.include_router(opa.router)
if get_settings().dev_routes_enabled:
    app.include_router(dev.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
