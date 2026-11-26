import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from services.temporal_worker.shared import ExtractResumeInput, FailResumeInput
from services.temporal_worker.workflows.extract_resume import ExtractResumeWorkflow

TASK_QUEUE = "voice"


class Fakes:
    def __init__(self, explode: bool = False, message: str = "the model is down") -> None:
        self.explode = explode
        self.message = message
        self.extracted: list[str] = []
        self.failed: list[FailResumeInput] = []

    def activities(self) -> list:
        @activity.defn(name="extract_resume")
        async def extract_resume(input: ExtractResumeInput) -> None:
            self.extracted.append(input.resume_id)
            if self.explode:
                raise RuntimeError(self.message)

        @activity.defn(name="fail_resume")
        async def fail_resume(input: FailResumeInput) -> None:
            self.failed.append(input)

        return [extract_resume, fail_resume]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


async def run(env: WorkflowEnvironment, fakes: Fakes) -> str:
    resume_id = str(uuid.uuid4())
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ExtractResumeWorkflow],
        activities=fakes.activities(),
    ):
        return await env.client.execute_workflow(
            ExtractResumeWorkflow.run,
            ExtractResumeInput(resume_id=resume_id),
            id=f"wf-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


async def test_a_successful_extraction_completes_the_workflow(env):
    fakes = Fakes()

    result = await run(env, fakes)

    assert result == "ready"
    assert len(fakes.extracted) == 1
    assert fakes.failed == []


async def test_an_exhausted_extraction_marks_the_resume_failed(env):
    fakes = Fakes(explode=True)

    result = await run(env, fakes)

    assert result == "failed"
    assert len(fakes.extracted) == 3
    assert len(fakes.failed) == 1
    assert fakes.failed[0].reason.startswith("resume extraction failed")


async def test_the_providers_own_error_reaches_the_recruiter(env):
    fakes = Fakes(explode=True, message="out of credits")

    await run(env, fakes)

    assert "out of credits" in fakes.failed[0].reason
