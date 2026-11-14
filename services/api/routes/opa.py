from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from services.api.bundle import bundle_data, pack, revision
from services.api.routes.internal_calls import require_internal_token

router = APIRouter(prefix="/internal/opa", tags=["internal"])


@router.get("/bundle.tar.gz", dependencies=[Depends(require_internal_token)])
async def opa_bundle(session: AsyncSession = Depends(get_session)) -> Response:
    data = await bundle_data(session)
    return Response(
        content=pack(data),
        media_type="application/gzip",
        headers={"ETag": f'"{revision(data)}"', "Cache-Control": "no-cache"},
    )
