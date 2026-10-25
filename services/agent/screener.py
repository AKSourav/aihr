import asyncio
import functools
import logging
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import dateparser
from livekit.agents import Agent, RunContext, function_tool

from core.schemas import CallContext
from services.agent.prompts import render_system_prompt

logger = logging.getLogger(__name__)

GOODBYE_WAIT_TIMEOUT_S = 8.0

_DATE_PARSE_EXECUTOR = ProcessPoolExecutor(max_workers=1)


class HRScreenerAgent(Agent):
    def __init__(self, call_context: CallContext) -> None:
        super().__init__(instructions=render_system_prompt(call_context))
        self.call_context = call_context
        self.answered_question_ids: set[str] = set()
        self.outcome_status: str | None = None
        self.callback_at: datetime | None = None
        self.recording_consent_granted: bool | None = None
        self.end_reason: str | None = None

    async def on_enter(self) -> None:
        self.session.generate_reply()

    async def _end_session_after_reply(self) -> None:
        session = self.session
        speech_created: asyncio.Future = asyncio.get_event_loop().create_future()

        def _on_speech_created(ev) -> None:
            if not speech_created.done():
                speech_created.set_result(ev.speech_handle)

        session.once("speech_created", _on_speech_created)
        try:
            speech_handle = await asyncio.wait_for(speech_created, timeout=GOODBYE_WAIT_TIMEOUT_S)
            await speech_handle
        except asyncio.TimeoutError:
            logger.warning("goodbye reply timed out, shutting down anyway")
        finally:
            session.off("speech_created", _on_speech_created)
            session.shutdown()

    @function_tool
    async def mark_question_answered(self, ctx: RunContext, question_id: str) -> str:
        """Mark a required question as answered once the candidate has responded to it.

        Args:
            question_id: The id of the question from the question plan, e.g. "q_notice".
        """
        self.answered_question_ids.add(question_id)
        logger.info("question answered", extra={"question_id": question_id})
        return f"Recorded {question_id} as answered. Continue to the next question."

    @function_tool
    async def recording_consent(self, ctx: RunContext, granted: bool) -> str:
        """Record whether the candidate agreed to have this call recorded.

        Args:
            granted: True if they agreed, False if they declined.
        """
        self.recording_consent_granted = granted
        logger.info("recording consent", extra={"granted": granted})
        if granted:
            return "Consent granted. Continue to the first question."
        return "Consent declined. Continue without recording, starting with the first question."

    @function_tool
    async def request_callback(self, ctx: RunContext, when: str) -> str:
        """The candidate asked to be called back at a different time instead of continuing now.

        Args:
            when: What the candidate said about timing, e.g. "tomorrow at 11am".
        """
        loop = asyncio.get_running_loop()
        parsed = await loop.run_in_executor(
            _DATE_PARSE_EXECUTOR,
            functools.partial(
                dateparser.parse,
                when,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "TIMEZONE": "Asia/Kolkata",
                    "TO_TIMEZONE": "UTC",
                },
            ),
        )
        self.callback_at = parsed
        self.outcome_status = "callback_requested"
        logger.info("callback requested", extra={"when": when, "parsed": str(parsed)})
        asyncio.create_task(self._end_session_after_reply())
        return "Say a brief, warm goodbye confirming the callback time, then the call will end."

    @function_tool
    async def detected_voicemail(self, ctx: RunContext) -> None:
        """Call this immediately if you hear a voicemail greeting, beep, or an automated
        'not reachable' message instead of a person. Do not say anything else, just call this.
        """
        self.outcome_status = "voicemail"
        logger.info("voicemail detected")
        self.session.shutdown()

    @function_tool
    async def end_call(self, ctx: RunContext, reason: str) -> str:
        """End the call after saying goodbye. Call this once the required questions and the
        candidate's questions have been covered.

        Args:
            reason: Why the call is ending, e.g. "completed all questions".
        """
        self.end_reason = reason
        if self.outcome_status is None:
            self.outcome_status = "completed"
        logger.info("end call", extra={"reason": reason})
        asyncio.create_task(self._end_session_after_reply())
        return "Say a brief, warm goodbye now, then the call will end."
