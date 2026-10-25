import logging

from livekit.agents import JobContext, WorkerOptions, cli

from core.logging import configure_logging
from core.settings import get_settings

logger = logging.getLogger(__name__)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("job connected", extra={"room": ctx.room.name})


def main() -> None:
    configure_logging()
    settings = get_settings()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=settings.agent_name,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    )


if __name__ == "__main__":
    main()
