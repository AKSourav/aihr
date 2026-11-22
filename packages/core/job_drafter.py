import json
import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from core.schemas import QuestionPlan, Rubric
from core.settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 2
MAX_DRAFT_TOKENS = 2000
DRAFT_TIMEOUT_S = 45.0

BEGIN_FENCE = "<<<BEGIN_JOB_DESCRIPTION>>>"
END_FENCE = "<<<END_JOB_DESCRIPTION>>>"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))
_FENCE_RE = re.compile(r"<+\s*/?\s*(BEGIN|END)_JOB_DESCRIPTION\s*>+", re.IGNORECASE)


class JobDraft(BaseModel):
    title: str
    rubric: Rubric
    question_plan: QuestionPlan


def _render_description(description: str) -> str:
    return f"{BEGIN_FENCE}\n{_FENCE_RE.sub('[redacted]', description)}\n{END_FENCE}"


def render_draft_prompt(title: str, max_minutes: int) -> str:
    template = _env.get_template("job_draft.md.j2")
    return template.render(
        job_title=title,
        max_minutes=max_minutes,
        begin_fence=BEGIN_FENCE,
        end_fence=END_FENCE,
    )


def _client() -> AsyncOpenAI:
    settings = get_settings()
    client_kwargs = {"api_key": settings.llm_api_key, "timeout": DRAFT_TIMEOUT_S}
    if settings.llm_base_url:
        client_kwargs["base_url"] = settings.llm_base_url
    return AsyncOpenAI(**client_kwargs)


async def draft_job(title: str, description: str) -> JobDraft:
    settings = get_settings()
    client = _client()

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": render_draft_prompt(title, settings.max_call_duration_min)},
        {"role": "user", "content": _render_description(description)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.chat.completions.create(
                model=settings.llm_summary_model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_DRAFT_TOKENS,
            )
        except Exception as exc:
            last_error = exc
            logger.warning("job draft request failed, retrying", extra={"attempt": attempt, "error": str(exc)})
            continue

        content = response.choices[0].message.content
        try:
            draft = JobDraft.model_validate(json.loads(str(content)) | {"title": title})
        except Exception as exc:
            last_error = exc
            logger.warning("invalid job draft JSON, retrying", extra={"attempt": attempt, "error": str(exc)})
            messages.append({"role": "assistant", "content": str(content)})
            messages.append(
                {
                    "role": "user",
                    "content": f"That response was rejected: {exc}. "
                    "Return only the JSON object described in the system message.",
                }
            )
            continue

        draft.question_plan.max_minutes = min(
            draft.question_plan.max_minutes, settings.max_call_duration_min
        )
        return draft

    raise ValueError(
        f"job draft LLM did not return a valid JobDraft after {MAX_ATTEMPTS} attempts"
    ) from last_error
