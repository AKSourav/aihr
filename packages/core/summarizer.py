import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.schemas import CallSummary, TokenUsage, Turn
from core.settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v2"
MAX_ATTEMPTS = 2
MAX_SUMMARY_TOKENS = 1500
SUMMARY_TIMEOUT_S = 45.0

MODERATION_MODEL = "omni-moderation-latest"

BEGIN_FENCE = "<<<BEGIN_TRANSCRIPT>>>"
END_FENCE = "<<<END_TRANSCRIPT>>>"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))
_FENCE_RE = re.compile(r"<+\s*/?\s*(BEGIN|END)_TRANSCRIPT\s*>+", re.IGNORECASE)


def _render_transcript(transcript: list[Turn]) -> str:
    body = "\n".join(f"{turn.speaker}: {_FENCE_RE.sub('[redacted]', turn.text)}" for turn in transcript)
    return f"{BEGIN_FENCE}\n{body}\n{END_FENCE}"


def _render_rubric(rubric: list[dict]) -> str:
    return "\n".join(f"- {item['dimension']} (weight {item['weight']}): {item['description']}" for item in rubric)


def _fact_type_hint(name: str) -> str:
    if name.endswith("_days"):
        return "integer (number of days)"
    if name.startswith(("open_to_", "is_", "has_")):
        return "boolean, true or false (not a sentence)"
    return "string"


def _fact_names(question_plan: dict) -> list[str]:
    names: list[str] = []
    for question in question_plan.get("questions", []):
        extract = question.get("extract")
        if not extract:
            continue
        names.extend(extract if isinstance(extract, list) else [extract])
    return names


def _render_fact_fields(question_plan: dict) -> str:
    names = _fact_names(question_plan)
    if not names:
        return "(none defined for this role)"
    return "\n".join(f'- "{name}": {_fact_type_hint(name)}' for name in names)


def _filter_summary(summary: CallSummary, rubric: list[dict], question_plan: dict) -> CallSummary:
    allowed_facts = set(_fact_names(question_plan))
    allowed_dimensions = {item["dimension"] for item in rubric}

    dropped_facts = [name for name in summary.facts if name not in allowed_facts]
    dropped_dimensions = [
        score.dimension for score in summary.rubric_scores if score.dimension not in allowed_dimensions
    ]
    if dropped_facts or dropped_dimensions:
        logger.warning(
            "dropped unexpected summary fields",
            extra={"dropped_facts": dropped_facts, "dropped_dimensions": dropped_dimensions},
        )

    summary.facts = {name: value for name, value in summary.facts.items() if name in allowed_facts}
    summary.rubric_scores = [
        score for score in summary.rubric_scores if score.dimension in allowed_dimensions
    ]
    return summary


def render_summary_prompt(job_title: str, rubric: list[dict], question_plan: dict) -> str:
    template = _env.get_template("summary.md.j2")
    return template.render(
        job_title=job_title,
        rubric_block=_render_rubric(rubric),
        fact_fields_block=_render_fact_fields(question_plan),
        begin_fence=BEGIN_FENCE,
        end_fence=END_FENCE,
    )


def _client() -> AsyncOpenAI:
    settings = get_settings()
    client_kwargs = {"api_key": settings.llm_api_key, "timeout": SUMMARY_TIMEOUT_S}
    if settings.llm_base_url:
        client_kwargs["base_url"] = settings.llm_base_url
    return AsyncOpenAI(**client_kwargs)


async def moderate(text: str) -> list[str]:
    try:
        response = await _client().moderations.create(model=MODERATION_MODEL, input=text)
    except Exception as exc:
        logger.warning("moderation unavailable, skipping", extra={"error": str(exc)})
        return []
    result = response.results[0]
    if not result.flagged:
        return []
    return sorted(name for name, hit in result.categories.model_dump().items() if hit)


async def summarize_transcript(
    job_title: str, rubric: list[dict], question_plan: dict, transcript: list[Turn]
) -> tuple[CallSummary, TokenUsage]:
    settings = get_settings()
    client = _client()

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": render_summary_prompt(job_title, rubric, question_plan)},
        {"role": "user", "content": _render_transcript(transcript)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.chat.completions.create(
                model=settings.llm_summary_model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_SUMMARY_TOKENS,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("summary request failed, retrying", extra={"attempt": attempt, "error": str(exc)})
            continue

        content = response.choices[0].message.content
        try:
            summary = CallSummary.model_validate_json(str(content))
        except Exception as exc:
            last_error = exc
            logger.warning("invalid summary JSON, retrying", extra={"attempt": attempt, "error": str(exc)})
            messages.append({"role": "assistant", "content": str(content)})
            messages.append(
                {
                    "role": "user",
                    "content": f"That response was rejected: {exc}. "
                    "Return only the JSON object described in the system message.",
                }
            )
            continue

        usage = TokenUsage(
            prompt_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
        )
        return _filter_summary(summary, rubric, question_plan), usage

    raise ValueError(
        f"summary LLM did not return a valid CallSummary after {MAX_ATTEMPTS} attempts"
    ) from last_error
