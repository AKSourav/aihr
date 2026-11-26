"""The assistant's turn loop. No database, no FastAPI — it is handed an execute callable."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from jinja2 import Environment, FileSystemLoader
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from core.schemas import TokenUsage

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
MAX_TOOL_ROUNDS = 4
MAX_TURN_TOKENS = 1200
ASSISTANT_TIMEOUT_S = 45.0
TURN_DEADLINE_S = 90.0
MAX_HISTORY_MESSAGES = 30
MAX_TOOL_RESULT_CHARS = 6000

BEGIN_FENCE = "<<<BEGIN_CANDIDATE_DATA>>>"
END_FENCE = "<<<END_CANDIDATE_DATA>>>"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "prompts"))
_FENCE_RE = re.compile(r"<+\s*/?\s*(BEGIN|END)_CANDIDATE_DATA\s*>+", re.IGNORECASE)


def fence(text: str) -> str:
    return f"{BEGIN_FENCE}\n{_FENCE_RE.sub('[redacted]', text)}\n{END_FENCE}"


def render_assistant_prompt(
    org_name: str, user_email: str, role: str, permissions: list[str], today: str, tools: list[str]
) -> str:
    template = _env.get_template("assistant.md.j2")
    return template.render(
        org_name=org_name,
        user_email=user_email,
        role=role,
        permissions=", ".join(permissions),
        today=today,
        tool_names=", ".join(tools),
        begin_fence=BEGIN_FENCE,
        end_fence=END_FENCE,
    )


@dataclass
class AssistantTurn:
    text: str
    steps: list[str] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    new_messages: list[dict] = field(default_factory=list)
    truncated: bool = False


def _truncate(result: str) -> str:
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    return result[:MAX_TOOL_RESULT_CHARS] + "\n…truncated"


async def run_turn(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    history: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam],
    execute: Callable[[str, str], Awaitable[str]],
) -> AssistantTurn:
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system_prompt}
    ]
    messages.extend(history[-MAX_HISTORY_MESSAGES:])

    steps: list[str] = []
    new_messages: list[dict] = []
    usage = TokenUsage()
    text = ""
    truncated = False

    async def loop() -> None:
        nonlocal text, truncated

        for round_no in range(MAX_TOOL_ROUNDS + 1):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto" if round_no < MAX_TOOL_ROUNDS else "none",
                parallel_tool_calls=False,
                max_completion_tokens=MAX_TURN_TOKENS,
            )

            usage.prompt_tokens += getattr(response.usage, "prompt_tokens", 0) or 0
            usage.completion_tokens += getattr(response.usage, "completion_tokens", 0) or 0

            message = response.choices[0].message
            # openai v3 returns a union of function and custom tool calls. Only function calls
            # carry a `.function`; a custom call must be ignored, not crashed on.
            calls = [
                cast(ChatCompletionMessageFunctionToolCall, call)
                for call in (message.tool_calls or [])
                if getattr(call, "type", "function") == "function"
            ]

            if not calls:
                text = message.content or ""
                new_messages.append({"role": "assistant", "content": text})
                return

            serialised = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in calls
            ]
            messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": serialised,
                    },
                )
            )
            new_messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": serialised,
                }
            )

            for call in calls:
                steps.append(call.function.name)
                result = _truncate(await execute(call.function.name, call.function.arguments))
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "content": result,
                    }
                )

    try:
        async with asyncio.timeout(TURN_DEADLINE_S):
            await loop()
    except TimeoutError:
        truncated = True
        logger.warning("assistant turn hit the deadline", extra={"steps": steps})
        text = text or (
            "I ran out of time working on that. Here is what I looked at: "
            + ", ".join(steps)
            + ". Ask me again and I will narrow it down."
        )
        new_messages.append({"role": "assistant", "content": text})

    return AssistantTurn(
        text=text, steps=steps, usage=usage, new_messages=new_messages, truncated=truncated
    )
