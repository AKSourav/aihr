import logging
from uuid import UUID

from temporalio import activity

from core.db import async_session
from core.livekit_rooms import create_room_and_dispatch
from core.models import Application, Call
from services.temporal_worker.shared import PlaceCallInput, PlaceCallResult

logger = logging.getLogger(__name__)


@activity.defn
async def place_call(input: PlaceCallInput) -> PlaceCallResult:
    call_id = UUID(input.call_id)

    async with async_session() as session:
        call = await session.get(Call, call_id)
        if call is None:
            application = await session.get(Application, UUID(input.application_id))
            if application is None:
                raise ValueError(f"application {input.application_id} not found")
            call = Call(
                id=call_id,
                org_id=application.org_id,
                application_id=application.id,
                attempt_no=input.attempt_no,
                call_mode=input.call_mode,
                status="dispatching",
            )
            session.add(call)
            await session.commit()
            await session.refresh(call)

        if call.room_name is not None:
            return PlaceCallResult(call_id=str(call.id), room_name=call.room_name)

        room_name = await create_room_and_dispatch(call.id)

        call.room_name = room_name
        call.status = "ringing"
        await session.commit()

    logger.info("call placed", extra={"call_id": str(call.id), "room_name": room_name})
    return PlaceCallResult(call_id=str(call.id), room_name=room_name)
