import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from services.temporal_worker.shared import (
    ExpireInviteInput,
    MarkUnreachableInput,
    PlaceCallInput,
    PlaceCallResult,
    ScreeningCallInput,
    SendInviteInput,
    SummarizeCallInput,
)
from services.temporal_worker.workflows.screening_call import ScreeningCallWorkflow

TASK_QUEUE = "voice"


class Fakes:
    def __init__(self) -> None:
        self.invited: list[SendInviteInput] = []
        self.expired: list[str] = []
        self.placed: list[PlaceCallInput] = []
        self.summarized: list[str] = []
        self.unreachable: list[str] = []

    def activities(self) -> list:
        @activity.defn(name="send_invite")
        async def send_invite(input: SendInviteInput) -> None:
            self.invited.append(input)

        @activity.defn(name="expire_invite")
        async def expire_invite(input: ExpireInviteInput) -> None:
            self.expired.append(input.call_id)

        @activity.defn(name="place_call")
        async def place_call(input: PlaceCallInput) -> PlaceCallResult:
            self.placed.append(input)
            return PlaceCallResult(call_id=input.call_id, room_name=f"call-{input.call_id}")

        @activity.defn(name="summarize_call")
        async def summarize_call(input: SummarizeCallInput) -> None:
            self.summarized.append(input.call_id)

        @activity.defn(name="mark_unreachable")
        async def mark_unreachable(input: MarkUnreachableInput) -> None:
            self.unreachable.append(input.application_id)

        return [send_invite, expire_invite, place_call, summarize_call, mark_unreachable]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


async def start(env: WorkflowEnvironment, fakes: Fakes, input: ScreeningCallInput):
    worker = Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ScreeningCallWorkflow],
        activities=fakes.activities(),
    )
    handle = await env.client.start_workflow(
        ScreeningCallWorkflow.run, input, id=f"wf-{uuid.uuid4()}", task_queue=TASK_QUEUE
    )
    return worker, handle


async def until(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def email_input(**overrides) -> ScreeningCallInput:
    values: dict[str, Any] = dict(application_id="app-1", call_mode="email", invite_ttl_hours=72, signal_timeout_min=45)
    values.update(overrides)
    return ScreeningCallInput(**values)


async def test_email_invite_that_is_joined_and_completed_is_summarized(env):
    fakes = Fakes()
    worker, handle = await start(env, fakes, email_input())

    async with worker:
        await until(lambda: len(fakes.invited) == 1)
        call_id = fakes.invited[0].call_id
        await handle.signal("call_joined", call_id)
        await handle.signal("call_completed", {"call_id": call_id, "status": "completed"})

        assert await handle.result() == "completed"

    assert fakes.summarized == [call_id]
    assert fakes.placed == []
    assert fakes.expired == []
    assert fakes.invited[0].ttl_hours == 72


async def test_unused_invites_expire_and_are_resent_until_unreachable(env):
    fakes = Fakes()
    worker, handle = await start(env, fakes, email_input())

    async with worker:
        assert await handle.result() == "unreachable"

    assert [i.attempt_no for i in fakes.invited] == [1, 2, 3]
    assert fakes.expired == [i.call_id for i in fakes.invited]
    assert fakes.unreachable == ["app-1"]
    assert fakes.placed == []


async def test_a_join_extends_the_wait_past_the_invite_ttl(env):
    fakes = Fakes()
    worker, handle = await start(env, fakes, email_input(invite_ttl_hours=1, signal_timeout_min=180))

    async with worker:
        await until(lambda: len(fakes.invited) == 1)
        call_id = fakes.invited[0].call_id
        await handle.signal("call_joined", call_id)
        # Two hours later, past the 1h invite TTL but inside the 3h signal timeout.
        await env.sleep(2 * 60 * 60)
        await handle.signal("call_completed", {"call_id": call_id, "status": "completed"})

        assert await handle.result() == "completed"

    assert fakes.expired == []
    assert len(fakes.invited) == 1


async def test_signals_for_a_previous_attempt_are_ignored(env):
    fakes = Fakes()
    worker, handle = await start(env, fakes, email_input(invite_ttl_hours=1))

    async with worker:
        await until(lambda: len(fakes.invited) == 1)
        stale = fakes.invited[0].call_id
        await env.sleep(61 * 60)
        await until(lambda: len(fakes.invited) == 2)
        await handle.signal("call_completed", {"call_id": stale, "status": "completed"})
        current = fakes.invited[1].call_id
        await handle.signal("call_completed", {"call_id": current, "status": "declined"})

        assert await handle.result() == "declined"

    assert fakes.summarized == []


async def test_phone_and_web_screenings_still_place_calls(env):
    fakes = Fakes()
    worker, handle = await start(
        env, fakes, ScreeningCallInput(application_id="app-2", call_mode="web", skip_business_hours=True)
    )

    async with worker:
        await until(lambda: len(fakes.placed) == 1)
        call_id = fakes.placed[0].call_id
        await handle.signal("call_completed", {"call_id": call_id, "status": "completed"})

        assert await handle.result() == "completed"

    assert fakes.invited == []
    assert fakes.summarized == [call_id]
    assert fakes.placed[0].call_mode == "web"
