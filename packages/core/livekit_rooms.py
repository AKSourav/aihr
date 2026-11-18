from livekit import api

from core.settings import get_settings

CANDIDATE_IDENTITY = "candidate"


def room_name_for(call_id: object) -> str:
    return f"call-{call_id}"


async def create_room_and_dispatch(call_id: object) -> str:
    """Create the LiveKit room for a call and dispatch the screening agent into it.

    The agent receives the call id as job metadata and fetches its context from the API.
    """
    settings = get_settings()
    room_name = room_name_for(call_id)
    async with api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    ) as lk:
        await lk.room.create_room(api.CreateRoomRequest(name=room_name))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name, room=room_name, metadata=str(call_id)
            )
        )
    return room_name


def candidate_join_token(room_name: str, display_name: str) -> str:
    settings = get_settings()
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(CANDIDATE_IDENTITY)
        .with_name(display_name)
        .with_grants(
            api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True)
        )
        .to_jwt()
    )
