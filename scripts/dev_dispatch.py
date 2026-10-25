import argparse
import asyncio
import urllib.parse

from livekit import api

from core.settings import get_settings


async def dispatch(call_id: str) -> None:
    settings = get_settings()
    room_name = f"call-{call_id}"

    async with api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
    ) as lk:
        await lk.room.create_room(api.CreateRoomRequest(name=room_name))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.agent_name,
                room=room_name,
                metadata=call_id,
            )
        )

    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("candidate")
        .with_name("Candidate")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    test_page_url = "http://localhost:8080/join.html?" + urllib.parse.urlencode(
        {"url": settings.livekit_url, "token": token}
    )

    print(f"Room: {room_name}")
    print(f"Join token: {token}")
    print(f"Test page (run `make webtest` first): {test_page_url}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a room and dispatch hr-screener for a seeded call.")
    parser.add_argument("call_id")
    args = parser.parse_args()
    asyncio.run(dispatch(args.call_id))
