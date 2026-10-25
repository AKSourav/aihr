import logging
from uuid import UUID

from livekit import api
from sqlalchemy import select
from temporalio import activity

from core.db import async_session
from core.models import Call
from core.settings import get_settings
from services.temporal_worker.shared import PlaceCallInput, PlaceCallResult

logger = logging.getLogger(__name__)


@activity.defn
async def place_call(input: PlaceCallInput) -> PlaceCallResult:
    settings = get_settings()
    application_id = UUID(input.application_id)

    async with async_session() as session:
        call = await session.scalar(
            select(Call).where(
                Call.application_id == application_id, Call.attempt_no == input.attempt_no
            )
        )
        if call is None:
            call = Call(
                application_id=application_id,
                attempt_no=input.attempt_no,
                call_mode=input.call_mode,
                status="dispatching",
            )
            session.add(call)
            await session.commit()
            await session.refresh(call)

        if call.room_name is not None:
            return PlaceCallResult(call_id=str(call.id), room_name=call.room_name)

        room_name = f"call-{call.id}"
        async with api.LiveKitAPI(
            settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
        ) as lk:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=settings.agent_name, room=room_name, metadata=str(call.id)
                )
            )

        call.room_name = room_name
        call.status = "ringing"
        await session.commit()

    logger.info("call placed", extra={"call_id": str(call.id), "room_name": room_name})
    return PlaceCallResult(call_id=str(call.id), room_name=room_name)
