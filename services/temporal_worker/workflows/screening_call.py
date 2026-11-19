import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from temporalio import workflow
from temporalio.common import RetryPolicy

from services.temporal_worker.shared import (
    ExpireInviteInput,
    MarkUnreachableInput,
    PlaceCallInput,
    ScreeningCallInput,
    SendInviteInput,
    SummarizeCallInput,
)

BUSINESS_HOURS_START = 9
BUSINESS_HOURS_END = 19
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

MAX_UNREACHABLE_ATTEMPTS = 3
MAX_TOTAL_ATTEMPTS = 6
NORMAL_RETRY_DELAYS = [timedelta(hours=1), timedelta(hours=4)]
FAST_RETRY_DELAYS = [timedelta(seconds=5), timedelta(seconds=15)]

ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3, maximum_interval=timedelta(minutes=1))

EMAIL_MODE = "email"


@workflow.defn
class ScreeningCallWorkflow:
    def __init__(self) -> None:
        self._outcome: dict | None = None
        self._expected_call_id: str | None = None
        self._joined = False

    @workflow.signal
    def call_completed(self, outcome: dict) -> None:
        if outcome.get("call_id") == self._expected_call_id:
            self._outcome = outcome

    @workflow.signal
    def call_joined(self, call_id: str) -> None:
        """The candidate opened their email invite and the agent was dispatched.

        From here the call gets the normal signal timeout, so a join just before the invite
        expires is not cut off by it.
        """
        if call_id == self._expected_call_id:
            self._joined = True

    async def _await_email_outcome(self, invite_ttl: timedelta, signal_timeout: timedelta) -> str:
        try:
            await workflow.wait_condition(
                lambda: self._outcome is not None or self._joined, timeout=invite_ttl
            )
        except asyncio.TimeoutError:
            return "expired"

        if self._outcome is None:
            try:
                await workflow.wait_condition(lambda: self._outcome is not None, timeout=signal_timeout)
            except asyncio.TimeoutError:
                return "lost"

        assert self._outcome is not None
        return self._outcome["status"]

    async def _wait_for_business_hours(self) -> None:
        while True:
            now = workflow.now().astimezone(BUSINESS_TZ)
            if BUSINESS_HOURS_START <= now.hour < BUSINESS_HOURS_END:
                return
            if now.hour < BUSINESS_HOURS_START:
                target = now.replace(hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0)
            else:
                target = (now + timedelta(days=1)).replace(
                    hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0
                )
            await asyncio.sleep((target - now).total_seconds())

    @workflow.run
    async def run(self, input: ScreeningCallInput) -> str:
        retry_delays = FAST_RETRY_DELAYS if input.fast_retries else NORMAL_RETRY_DELAYS
        signal_timeout = timedelta(minutes=input.signal_timeout_min)

        attempt_no = 1
        unreachable_count = 0

        email_mode = input.call_mode == EMAIL_MODE

        while attempt_no <= MAX_TOTAL_ATTEMPTS:
            if email_mode:
                call_id = str(workflow.uuid4())
                self._outcome = None
                self._joined = False
                self._expected_call_id = call_id
                await workflow.execute_activity(
                    "send_invite",
                    SendInviteInput(
                        call_id=call_id,
                        application_id=input.application_id,
                        attempt_no=attempt_no,
                        ttl_hours=input.invite_ttl_hours,
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )
                status = await self._await_email_outcome(
                    timedelta(hours=input.invite_ttl_hours), signal_timeout
                )
                if status == "expired":
                    await workflow.execute_activity(
                        "expire_invite",
                        ExpireInviteInput(call_id=call_id),
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=ACTIVITY_RETRY_POLICY,
                    )
            else:
                if not input.skip_business_hours:
                    await self._wait_for_business_hours()

                call_id = str(workflow.uuid4())
                await workflow.execute_activity(
                    "place_call",
                    PlaceCallInput(
                        call_id=call_id,
                        application_id=input.application_id,
                        attempt_no=attempt_no,
                        call_mode=input.call_mode,
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )

                self._outcome = None
                self._expected_call_id = call_id
                try:
                    await workflow.wait_condition(lambda: self._outcome is not None, timeout=signal_timeout)
                    if self._outcome:
                        status = self._outcome["status"]
                    else:
                        raise Exception("unexpected outcome is none")
                except asyncio.TimeoutError:
                    status = "lost"

            if status == "completed":
                await workflow.execute_activity(
                    "summarize_call",
                    SummarizeCallInput(call_id=call_id),
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )
                return "completed"
            if status == "declined":
                return "declined"

            if status == "callback_requested":
                callback_at_raw = self._outcome.get("callback_at") if self._outcome else None
                if callback_at_raw:
                    callback_at = datetime.fromisoformat(callback_at_raw)
                    delay_seconds = (callback_at - workflow.now()).total_seconds()
                    if delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
                attempt_no += 1
                continue

            unreachable_count += 1
            if unreachable_count >= MAX_UNREACHABLE_ATTEMPTS:
                break
            # An expired email invite has already waited out its TTL, so re-invite straight away.
            if not email_mode:
                delay_index = min(unreachable_count - 1, len(retry_delays) - 1)
                await asyncio.sleep(retry_delays[delay_index].total_seconds())
            attempt_no += 1

        await workflow.execute_activity(
            "mark_unreachable",
            MarkUnreachableInput(application_id=input.application_id),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=ACTIVITY_RETRY_POLICY,
        )
        return "unreachable"
