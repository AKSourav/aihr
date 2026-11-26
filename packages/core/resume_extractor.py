import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.llm import client as llm_client
from core.schemas import ResolvedProvider, ResumeExtract, TokenUsage
from core.settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 2
MAX_EXTRACT_TOKENS = 2000
EXTRACT_TIMEOUT_S = 60.0

BEGIN_FENCE = "<<<BEGIN_RESUME>>>"
END_FENCE = "<<<END_RESUME>>>"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))
_FENCE_RE = re.compile(r"<+\s*/?\s*(BEGIN|END)_RESUME\s*>+", re.IGNORECASE)


def _render_resume(text: str) -> str:
    return f"{BEGIN_FENCE}\n{_FENCE_RE.sub('[redacted]', text)}\n{END_FENCE}"


def render_extract_prompt() -> str:
    template = _env.get_template("resume_extract.md.j2")
    return template.render(begin_fence=BEGIN_FENCE, end_fence=END_FENCE)


def _client(llm: ResolvedProvider | None = None) -> AsyncOpenAI:
    return llm_client(llm, timeout=EXTRACT_TIMEOUT_S)


async def extract_resume(
    text: str, llm: ResolvedProvider | None = None
) -> tuple[ResumeExtract, TokenUsage]:
    settings = get_settings()
    client = _client(llm)
    model = (llm.model if llm else "") or settings.llm_summary_model

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": render_extract_prompt()},
        {"role": "user", "content": _render_resume(text)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_EXTRACT_TOKENS,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "resume extraction request failed, retrying",
                extra={"attempt": attempt, "error": str(exc)},
            )
            continue

        content = response.choices[0].message.content
        try:
            extract = ResumeExtract.model_validate_json(str(content))
        except Exception as exc:
            last_error = exc
            logger.warning(
                "invalid resume extract JSON, retrying",
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
        return extract, usage

    raise ValueError(
        f"resume extraction LLM did not return a valid ResumeExtract after {MAX_ATTEMPTS} attempts"
    ) from last_error
