from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from services.temporal_worker.shared import (
    ExtractResumeInput,
    FailResumeInput,
    ScoreResumeInput,
)

ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3, maximum_interval=timedelta(minutes=1))

EXTRACT_TIMEOUT = timedelta(seconds=180)
SCORE_TIMEOUT = timedelta(seconds=120)
FAIL_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class ExtractResumeWorkflow:
    @workflow.run
    async def run(self, input: ExtractResumeInput) -> str:
        try:
            application_id = await workflow.execute_activity(
                "extract_resume",
                ExtractResumeInput(resume_id=input.resume_id),
                start_to_close_timeout=EXTRACT_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            if application_id:
                await workflow.execute_activity(
                    "score_resume",
                    ScoreResumeInput(application_id=application_id),
                    start_to_close_timeout=SCORE_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )
        except Exception as exc:
            # Carry the provider's own words through: "out of credits" and "model not found"
            # are things a recruiter can act on, where a bare "failed" is a dead end.
            await workflow.execute_activity(
                "fail_resume",
                FailResumeInput(resume_id=input.resume_id, reason=_reason(exc)),
                start_to_close_timeout=FAIL_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            return "failed"
        return "ready"


def _reason(exc: BaseException) -> str:
    cause: BaseException = exc
    while True:
        nested = getattr(cause, "cause", None)
        if not isinstance(nested, BaseException):
            break
        cause = nested
    detail = str(cause).strip() or type(cause).__name__
    return f"resume extraction failed: {detail}"[:400]
