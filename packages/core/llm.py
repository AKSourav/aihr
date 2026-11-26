from openai import AsyncOpenAI

from core.schemas import ResolvedProvider
from core.settings import get_settings

DEFAULT_TIMEOUT_S = 45.0


def provider_detail(exc: BaseException, prefix: str) -> str:
    """A readable reason for an LLM failure, in the provider's own words where it gave any.

    The retry loops raise a ValueError and chain the provider error as __cause__, so the useful
    text is usually one level down. "Out of credits" and "unknown model" are things a recruiter
    can act on; an opaque "it failed" is a dead end.
    """
    cause: BaseException = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__

    message = getattr(cause, "message", None)
    body = getattr(cause, "body", None)
    if not message and isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")

    detail = str(message or cause).strip() or type(cause).__name__
    return f"{prefix}: {detail}"[:400]


def client(llm: ResolvedProvider | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> AsyncOpenAI:
    settings = get_settings()
    api_key = llm.api_key if llm else settings.llm_api_key
    base_url = (llm.base_url if llm else None) or settings.llm_base_url
    kwargs = {"api_key": api_key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)
