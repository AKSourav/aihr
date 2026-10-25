import asyncio
import logging
import os
from datetime import datetime, timezone

from livekit.agents import AgentSession, JobContext, JobExecutorType, WorkerOptions, cli
from livekit.agents.inference import TurnDetector
from livekit.plugins import cartesia, deepgram, openai, silero

from core.logging import configure_logging
from core.schemas import CallMode, CallOutcome, CallStatus
from core.settings import get_settings
from services.agent.context import fetch_call_context
from services.agent.reporter import report_outcome
from services.agent.screener import HRScreenerAgent
from services.agent.transcript import TranscriptCollector

logger = logging.getLogger(__name__)

WEB_JOIN_TIMEOUT_S = 120.0


def print_transcript(turns: list) -> None:
    print("\n--- Transcript ---")
    for turn in turns:
        print(f"[{turn.started_at.isoformat()}] {turn.speaker}: {turn.text}")
    print("--- End transcript ---\n")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("job connected", extra={"room": ctx.room.name})

    call_id = ctx.job.metadata or os.environ.get("CONSOLE_CALL_ID")
    if not call_id:
        raise RuntimeError("no call_id in job metadata and CONSOLE_CALL_ID is not set")

    call_context = await fetch_call_context(call_id)
    settings = get_settings()

    if call_context.call_mode == CallMode.web:
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=WEB_JOIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.info("no participant joined in time", extra={"call_id": call_id})
            await report_outcome(
                CallOutcome(
                    call_id=call_context.call_id,
                    status=CallStatus.no_answer,
                    duration_seconds=0,
                )
            )
            return

    tts_kwargs = {"api_key": settings.tts_api_key}
    if settings.tts_voice_id:
        tts_kwargs["voice"] = settings.tts_voice_id

    llm_kwargs = {
        "model": settings.llm_call_model,
        "api_key": settings.llm_api_key,
        "max_completion_tokens": 300,
    }
    if settings.llm_base_url:
        llm_kwargs["base_url"] = settings.llm_base_url

    session = AgentSession(
        stt=deepgram.STT(api_key=settings.deepgram_api_key),
        llm=openai.LLM(**llm_kwargs),
        tts=cartesia.TTS(**tts_kwargs),
        vad=silero.VAD.load(),
        turn_detection=TurnDetector(),
    )

    transcript = TranscriptCollector()
    transcript.attach(session)

    closed = asyncio.Event()
    session.on("close", lambda ev: closed.set())

    agent = HRScreenerAgent(call_context)
    started_at = datetime.now(timezone.utc)
    await session.start(agent=agent, room=ctx.room)
    await closed.wait()

    duration_seconds = int((datetime.now(timezone.utc) - started_at).total_seconds())
    status = CallStatus(agent.outcome_status) if agent.outcome_status else CallStatus.failed

    print_transcript(transcript.turns)
    logger.info(
        "call finished",
        extra={
            "outcome_status": status.value,
            "answered_question_ids": sorted(agent.answered_question_ids),
            "callback_at": str(agent.callback_at),
            "recording_consent_granted": agent.recording_consent_granted,
        },
    )

    await report_outcome(
        CallOutcome(
            call_id=call_context.call_id,
            status=status,
            transcript=transcript.turns,
            callback_at=agent.callback_at,
            answered_question_ids=sorted(agent.answered_question_ids),
            duration_seconds=duration_seconds,
        )
    )


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
            job_executor_type=JobExecutorType.PROCESS,
        )
    )


if __name__ == "__main__":
    main()
