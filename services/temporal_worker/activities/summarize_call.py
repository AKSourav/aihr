import logging
from uuid import UUID

from sqlalchemy import select
from temporalio import activity

from core.db import async_session
from core.models import Call
from core.models import CallSummary as CallSummaryRow
from core.models import Score
from core.schemas import Turn
from core.settings import get_settings
from core.storage import Storage
from core.summarizer import PROMPT_VERSION, summarize_transcript
from services.temporal_worker.shared import SummarizeCallInput

logger = logging.getLogger(__name__)


def _weighted_overall(rubric: list[dict], rubric_scores: list) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    for item in rubric:
        match = next((rs for rs in rubric_scores if rs.dimension == item["dimension"]), None)
        if match is None:
            continue
        weighted_sum += match.score * item["weight"]
        total_weight += item["weight"]
    if total_weight == 0:
        return 0.0
    return round(weighted_sum / total_weight, 4)


@activity.defn
async def summarize_call(input: SummarizeCallInput) -> None:
    call_id = UUID(input.call_id)
    settings = get_settings()

    async with async_session() as session:
        call = await session.get(Call, call_id)
        if call is None or call.transcript_url is None:
            logger.warning("no transcript to summarize", extra={"call_id": input.call_id})
            return

        existing = await session.scalar(select(CallSummaryRow).where(CallSummaryRow.call_id == call_id))
        if existing is not None:
            return

        job = call.application.job
        key = call.transcript_url.removeprefix(f"s3://{settings.s3_bucket}/")
        transcript_data = Storage().download_json(key)
        transcript = [Turn.model_validate(turn) for turn in transcript_data]

        summary = await summarize_transcript(job.title, job.rubric, job.question_plan, transcript)

        session.add(
            CallSummaryRow(
                call_id=call_id,
                summary=summary.summary,
                rubric_scores=[rs.model_dump() for rs in summary.rubric_scores],
                facts=summary.facts,
                red_flags=summary.red_flags,
                candidate_questions=summary.candidate_questions,
                confidence=summary.confidence,
                model_version=settings.llm_summary_model,
                prompt_version=PROMPT_VERSION,
            )
        )
        session.add(
            Score(
                application_id=call.application_id,
                stage="post_call",
                breakdown={
                    "overall": _weighted_overall(job.rubric, summary.rubric_scores),
                    **{rs.dimension: rs.score for rs in summary.rubric_scores},
                },
            )
        )
        await session.commit()

    logger.info("call summarized", extra={"call_id": input.call_id})
