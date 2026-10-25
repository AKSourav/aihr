import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from temporalio import workflow

from services.temporal_worker.shared import (
    MarkUnreachableInput,
    PlaceCallInput,
    PlaceCallResult,
    ScreeningCallInput,
)

BUSINESS_HOURS_START = 9
BUSINESS_HOURS_END = 19
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

MAX_UNREACHABLE_ATTEMPTS = 3
MAX_TOTAL_ATTEMPTS = 6
NORMAL_RETRY_DELAYS = [timedelta(hours=1), timedelta(hours=4)]
FAST_RETRY_DELAYS = [timedelta(seconds=5), timedelta(seconds=15)]
SIGNAL_TIMEOUT = timedelta(minutes=45)


@workflow.defn
class ScreeningCallWorkflow:
    def __init__(self) -> None:
        self._outcome: dict | None = None
        self._expected_call_id: str | None = None

    @workflow.signal
    def call_completed(self, outcome: dict) -> None:
        if outcome.get("call_id") == self._expected_call_id:
            self._outcome = outcome

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

        attempt_no = 1
        unreachable_count = 0

        while attempt_no <= MAX_TOTAL_ATTEMPTS:
            if not input.skip_business_hours:
                await self._wait_for_business_hours()

            place_result = await workflow.execute_activity(
                "place_call",
                PlaceCallInput(
                    application_id=input.application_id,
                    attempt_no=attempt_no,
                    call_mode=input.call_mode,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                result_type=PlaceCallResult,
            )

            self._outcome = None
            self._expected_call_id = place_result.call_id
            try:
                await workflow.wait_condition(lambda: self._outcome is not None, timeout=SIGNAL_TIMEOUT)
                if self._outcome:
                    status = self._outcome["status"]
                else:
                    raise Exception("unexpected outcome is none")
            except asyncio.TimeoutError:
                status = "lost"

            if status == "completed":
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
            delay_index = min(unreachable_count - 1, len(retry_delays) - 1)
            await asyncio.sleep(retry_delays[delay_index].total_seconds())
            attempt_no += 1

        await workflow.execute_activity(
            "mark_unreachable",
            MarkUnreachableInput(application_id=input.application_id),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return "unreachable"
