import json
import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.llm import client as llm_client
from core.schemas import ResolvedProvider, ResumeScore, TokenUsage
from core.settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 2
MAX_SCORE_TOKENS = 1200
SCORE_TIMEOUT_S = 45.0
MAX_RESUME_CHARS = 20_000

BEGIN_FENCE = "<<<BEGIN_RESUME>>>"
END_FENCE = "<<<END_RESUME>>>"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))
_FENCE_RE = re.compile(r"<+\s*/?\s*(BEGIN|END)_RESUME\s*>+", re.IGNORECASE)


def _render_rubric(rubric: list[dict]) -> str:
    return "\n".join(
        f"- {item['dimension']} (weight {item['weight']}): {item['description']}"
        for item in rubric
    )


def _render_resume(text: str, data: dict | None) -> str:
    body = _FENCE_RE.sub("[redacted]", text)[:MAX_RESUME_CHARS]
    if data:
        structured = _FENCE_RE.sub("[redacted]", json.dumps(data, default=str))
        body = f"{body}\n\nExtracted fields:\n{structured[:MAX_RESUME_CHARS]}"
    return f"{BEGIN_FENCE}\n{body}\n{END_FENCE}"


def render_score_prompt(job_title: str, rubric: list[dict]) -> str:
    template = _env.get_template("resume_score.md.j2")
    return template.render(
        job_title=job_title,
        rubric_block=_render_rubric(rubric),
        begin_fence=BEGIN_FENCE,
        end_fence=END_FENCE,
    )


def _filter_scores(score: ResumeScore, rubric: list[dict]) -> ResumeScore:
    allowed = {item["dimension"] for item in rubric}
    dropped = [rs.dimension for rs in score.rubric_scores if rs.dimension not in allowed]
    if dropped:
        logger.warning("dropped unexpected resume dimensions", extra={"dropped": dropped})
    score.rubric_scores = [rs for rs in score.rubric_scores if rs.dimension in allowed]
    return score


def _client(llm: ResolvedProvider | None = None) -> AsyncOpenAI:
    return llm_client(llm, timeout=SCORE_TIMEOUT_S)


async def score_resume(
    job_title: str,
    rubric: list[dict],
    resume_text: str,
    resume_data: dict | None = None,
    llm: ResolvedProvider | None = None,
) -> tuple[ResumeScore, TokenUsage]:
    settings = get_settings()
    client = _client(llm)
    model = (llm.model if llm else "") or settings.llm_summary_model

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": render_score_prompt(job_title, rubric)},
        {"role": "user", "content": _render_resume(resume_text, resume_data)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_SCORE_TOKENS,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "resume score request failed, retrying",
                extra={"attempt": attempt, "error": str(exc)},
            )
            continue

        content = response.choices[0].message.content
        try:
            score = ResumeScore.model_validate_json(str(content))
        except Exception as exc:
            last_error = exc
            logger.warning(
                "invalid resume score JSON, retrying",
                extra={"attempt": attempt, "error": str(exc)},
            )
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
        return _filter_scores(score, rubric), usage

    raise ValueError(
        f"resume scoring LLM did not return a valid ResumeScore after {MAX_ATTEMPTS} attempts"
    ) from last_error
