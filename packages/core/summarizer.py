import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI

from core.schemas import CallSummary, Turn
from core.settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 2
MAX_SUMMARY_TOKENS = 1500

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))


def _render_transcript(transcript: list[Turn]) -> str:
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in transcript)


def _render_rubric(rubric: list[dict]) -> str:
    return "\n".join(f"- {item['dimension']} (weight {item['weight']}): {item['description']}" for item in rubric)


def _fact_type_hint(name: str) -> str:
    if name.endswith("_days"):
        return "integer (number of days)"
    if name.startswith(("open_to_", "is_", "has_")):
        return "boolean, true or false (not a sentence)"
    return "string"


def _render_fact_fields(question_plan: dict) -> str:
    names: list[str] = []
    for question in question_plan.get("questions", []):
        extract = question.get("extract")
        if not extract:
            continue
        names.extend(extract if isinstance(extract, list) else [extract])
    if not names:
        return "(none defined for this role)"
    return "\n".join(f'- "{name}": {_fact_type_hint(name)}' for name in names)


def render_summary_prompt(job_title: str, rubric: list[dict], question_plan: dict, transcript: list[Turn]) -> str:
    template = _env.get_template("summary.md.j2")
    return template.render(
        job_title=job_title,
        rubric_block=_render_rubric(rubric),
        fact_fields_block=_render_fact_fields(question_plan),
        transcript_block=_render_transcript(transcript),
    )


async def summarize_transcript(
    job_title: str, rubric: list[dict], question_plan: dict, transcript: list[Turn]
) -> CallSummary:
    settings = get_settings()
    client_kwargs = {"api_key": settings.llm_api_key}
    if settings.llm_base_url:
        client_kwargs["base_url"] = settings.llm_base_url
    client = AsyncOpenAI(**client_kwargs)

    prompt = render_summary_prompt(job_title, rubric, question_plan, transcript)

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = await client.chat.completions.create(
            model=settings.llm_summary_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_completion_tokens=MAX_SUMMARY_TOKENS,
        )
        content = response.choices[0].message.content
        try:
            return CallSummary.model_validate_json(str(content))
        except Exception as exc:
            last_error = exc
            logger.warning("invalid summary JSON, retrying", extra={"attempt": attempt, "error": str(exc)})

    raise ValueError(
        f"summary LLM did not return valid CallSummary JSON after {MAX_ATTEMPTS} attempts"
    ) from last_error
