import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.logging import configure_logging
from core.settings import get_settings
from services.api.routes import dev, internal_calls

configure_logging()

app = FastAPI(title="aihr-api")

_hits: dict[str, list[float]] = defaultdict(list)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    limit = get_settings().rate_limit_per_minute
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()

    recent = [hit for hit in _hits[client] if now - hit < 60]
    if len(recent) >= limit:
        _hits[client] = recent
        return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})

    recent.append(now)
    _hits[client] = recent
    return await call_next(request)


app.include_router(internal_calls.router)
if get_settings().dev_routes_enabled:
    app.include_router(dev.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
