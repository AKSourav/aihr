import logging
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from core.fairness import protected_terms
from core.llm import client as llm_client
from core.schemas import CohortSummary, ResolvedProvider, TokenUsage
from core.settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_ATTEMPTS = 2
MAX_COHORT_TOKENS = 2000
COHORT_TIMEOUT_S = 45.0
MAX_RESUME_CHARS = 6000

BEGIN_FENCE = "<<<BEGIN_RESUMES>>>"
END_FENCE = "<<<END_RESUMES>>>"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))
_FENCE_RE = re.compile(r"<+\s*/?\s*(BEGIN|END)_RESUMES\s*>+", re.IGNORECASE)


def _scrub(value: str) -> str:
    return _FENCE_RE.sub("[redacted]", value)


def _render_rubric(rubric: list[dict]) -> str:
    return "\n".join(
        f"- {item['dimension']} (weight {item['weight']}): {item['description']}"
        for item in rubric
    )


def _render_cohort(candidates: list[tuple[str, str]]) -> str:
    sections = [
        f"### Candidate {index}: {_scrub(name)}\n{_scrub(text)[:MAX_RESUME_CHARS]}"
        for index, (name, text) in enumerate(candidates, start=1)
    ]
    return f"{BEGIN_FENCE}\n" + "\n\n".join(sections) + f"\n{END_FENCE}"


def render_cohort_prompt(job_title: str, rubric: list[dict]) -> str:
    template = _env.get_template("cohort_summary.md.j2")
    return template.render(
        job_title=job_title,
        rubric_block=_render_rubric(rubric),
        begin_fence=BEGIN_FENCE,
        end_fence=END_FENCE,
    )


def _filter_cohort(summary: CohortSummary, names: set[str]) -> CohortSummary:
    dropped = [note.name for note in summary.candidates if note.name not in names]
    if dropped:
        logger.warning("dropped notes for unrequested candidates", extra={"dropped": dropped})

    kept = []
    for note in summary.candidates:
        if note.name not in names:
            continue
        note.strengths = [item for item in note.strengths if not protected_terms(item)]
        note.gaps = [item for item in note.gaps if not protected_terms(item)]
        kept.append(note)
    summary.candidates = kept
    return summary


def _client(llm: ResolvedProvider | None = None) -> AsyncOpenAI:
    return llm_client(llm, timeout=COHORT_TIMEOUT_S)


async def summarize_cohort(
    job_title: str,
    rubric: list[dict],
    candidates: list[tuple[str, str]],
    llm: ResolvedProvider | None = None,
) -> tuple[CohortSummary, TokenUsage]:
    settings = get_settings()
    client = _client(llm)
    model = (llm.model if llm else "") or settings.llm_summary_model
    names = {_scrub(name) for name, _ in candidates}

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": render_cohort_prompt(job_title, rubric)},
        {"role": "user", "content": _render_cohort(candidates)},
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_COHORT_TOKENS,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "cohort summary request failed, retrying",
                extra={"attempt": attempt, "error": str(exc)},
            )
            continue

        content = response.choices[0].message.content
        try:
            summary = CohortSummary.model_validate_json(str(content))
        except Exception as exc:
            last_error = exc
            logger.warning(
                "invalid cohort summary JSON, retrying",
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
        return _filter_cohort(summary, names), usage

    raise ValueError(
        f"cohort summary LLM did not return a valid CohortSummary after {MAX_ATTEMPTS} attempts"
    ) from last_error
