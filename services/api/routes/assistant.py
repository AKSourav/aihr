import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from openai import OpenAIError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.assistant import (
    PROMPT_VERSION,
    AssistantTurn,
    render_assistant_prompt,
    run_turn,
)
from core.assistant_tools import TOOLS, reset_conversation, set_conversation, tool_schemas
from core.db import get_session
from core.fairness import protected_terms
from core.llm import client as llm_client
from core.llm import provider_detail
from core.models import (
    Application,
    AssistantConversation,
    AssistantMessage,
    AssistantPendingAction,
    AuditEvent,
    Organization,
)
from core.permissions import Permission, ROLE_PERMISSIONS
from core.providers import ProviderNotConfigured, resolve_org_llm
from core.settings import get_settings
from services.api.authz import TenantContext, ensure, require
from services.api.routes.screenings import start_screening_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{org_id}", tags=["assistant"])

ACTION_TTL = timedelta(hours=1)
MAX_MESSAGES_PER_CONVERSATION = 100
TITLE_CHARS = 80


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None


class PendingActionOut(BaseModel):
    pending_action_id: UUID
    action: str
    reason: str
    applications: list[dict]


class MessageOut(BaseModel):
    conversation_id: UUID
    text: str
    steps: list[str]
    pending_action: PendingActionOut | None = None
    fairness_flags: list[str] = []
    truncated: bool = False


class HistoryMessageOut(BaseModel):
    role: str
    content: str | None
    steps: list[str] = []
    fairness_flags: list[str] = []
    created_at: datetime


class HistoryOut(BaseModel):
    conversation_id: UUID | None
    messages: list[HistoryMessageOut]
    pending_action: PendingActionOut | None = None


class ConfirmRequest(BaseModel):
    confirm: bool = True


class ConfirmOut(BaseModel):
    action: str
    status: str
    results: list[dict] = []


def _retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=get_settings().assistant_retention_days)


async def _conversation(
    session: AsyncSession, context: TenantContext, conversation_id: UUID | None, title: str
) -> AssistantConversation:
    if conversation_id is not None:
        conversation = await session.scalar(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.org_id == context.org_id,
                AssistantConversation.user_id == context.user.id,
            )
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="not found")
        return conversation

    conversation = AssistantConversation(
        id=uuid.uuid4(),
        org_id=context.org_id,
        user_id=context.user.id,
        title=title[:TITLE_CHARS],
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _history(
    session: AsyncSession, conversation: AssistantConversation
) -> list[AssistantMessage]:
    return list(
        (
            await session.scalars(
                select(AssistantMessage)
                .where(
                    AssistantMessage.conversation_id == conversation.id,
                    AssistantMessage.org_id == conversation.org_id,
                    AssistantMessage.created_at >= _retention_cutoff(),
                )
                .order_by(AssistantMessage.created_at, AssistantMessage.id)
            )
        ).all()
    )


def _as_openai(rows: list[AssistantMessage]) -> list[ChatCompletionMessageParam]:
    messages: list[ChatCompletionMessageParam] = []
    for row in rows:
        if row.role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": row.tool_call_id or "",
                    "content": row.content or "",
                }
            )
        elif row.role == "assistant" and row.tool_calls:
            messages.append(
                cast(
                    ChatCompletionMessageParam,
                    {
                        "role": "assistant",
                        "content": row.content,
                        "tool_calls": row.tool_calls,
                    },
                )
            )
        elif row.role == "assistant":
            messages.append({"role": "assistant", "content": row.content or ""})
        else:
            messages.append({"role": "user", "content": row.content or ""})
    return messages


def _executor(session: AsyncSession, context: TenantContext):
    async def execute(name: str, arguments: str) -> str:
        tool = TOOLS.get(name)
        if tool is None:
            return json.dumps({"error": "unknown_tool", "tool": name})

        try:
            await ensure(session, context, tool.permission)
        except HTTPException:
            return json.dumps(
                {"error": "permission_denied", "permission": str(tool.permission)}
            )

        try:
            args = tool.args.model_validate_json(arguments or "{}")
        except Exception as exc:
            return json.dumps({"error": "invalid_arguments", "detail": str(exc)[:400]})

        try:
            result = await tool.run(session, context.org_id, context.user.id, args)
        except Exception as exc:
            logger.exception("assistant tool failed", extra={"tool": name})
            return json.dumps({"error": "tool_failed", "detail": str(exc)[:200]})

        return json.dumps(result, default=str)

    return execute


async def _pending_for(
    session: AsyncSession, conversation: AssistantConversation
) -> AssistantPendingAction | None:
    return await session.scalar(
        select(AssistantPendingAction)
        .where(
            AssistantPendingAction.conversation_id == conversation.id,
            AssistantPendingAction.org_id == conversation.org_id,
            AssistantPendingAction.status == "pending",
            AssistantPendingAction.created_at >= datetime.now(timezone.utc) - ACTION_TTL,
        )
        .order_by(AssistantPendingAction.created_at.desc())
        .limit(1)
    )


async def _pending_out(
    session: AsyncSession, action: AssistantPendingAction | None
) -> PendingActionOut | None:
    if action is None:
        return None

    ids = [UUID(value) for value in action.payload.get("application_ids", [])]
    rows = (
        await session.scalars(
            select(Application).where(
                Application.org_id == action.org_id, Application.id.in_(ids)
            )
        )
    ).all()

    return PendingActionOut(
        pending_action_id=action.id,
        action=action.action,
        reason=action.payload.get("reason", ""),
        applications=[
            {
                "application_id": str(a.id),
                "candidate_name": f"{a.candidate.first_name} {a.candidate.last_name}".strip(),
                "candidate_email": a.candidate.email,
                "job_title": a.job.title,
            }
            for a in rows
        ],
    )


@router.post("/assistant/messages", response_model=MessageOut)
async def send_message(
    body: MessageRequest,
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    organization = await session.get(Organization, context.org_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="not found")

    llm = None
    try:
        llm = await resolve_org_llm(session, organization)
    except ProviderNotConfigured:
        llm = None

    settings = get_settings()
    api_key = llm.api_key if llm else settings.llm_api_key
    model = (llm.model if llm else "") or settings.llm_summary_model
    if not api_key or not model:
        raise HTTPException(
            status_code=503, detail="the assistant is not configured on this server"
        )

    conversation = await _conversation(session, context, body.conversation_id, body.message)
    rows = await _history(session, conversation)
    if len(rows) >= MAX_MESSAGES_PER_CONVERSATION:
        raise HTTPException(
            status_code=429, detail="this conversation is full; start a new chat"
        )

    # Postgres now() is transaction-scoped, so every row written in this request would share a
    # created_at and fall back to comparing random UUIDs. Conversation order is the model's
    # context, so stamp it explicitly and strictly increasing.
    base = datetime.now(timezone.utc)
    session.add(
        AssistantMessage(
            id=uuid.uuid4(),
            org_id=context.org_id,
            conversation_id=conversation.id,
            role="user",
            content=body.message,
            created_at=base,
        )
    )
    await session.flush()

    permissions = [str(p) for p in ROLE_PERMISSIONS.get(context.role or "", [])]
    system_prompt = render_assistant_prompt(
        org_name=organization.name,
        user_email=context.user.email,
        role=context.role or "break-glass",
        permissions=permissions,
        today=datetime.now(timezone.utc).date().isoformat(),
        tools=list(TOOLS),
    )

    token = set_conversation(conversation.id)
    try:
        turn: AssistantTurn = await run_turn(
            client=llm_client(llm),
            model=model,
            system_prompt=system_prompt,
            history=_as_openai(rows) + [{"role": "user", "content": body.message}],
            tools=tool_schemas(),
            execute=_executor(session, context),
        )
    except OpenAIError as exc:
        # Credits, an unknown model, or a base_url that cannot do tool calling all land here.
        # The recruiter can act on the provider's own words; a bare 500 tells them nothing.
        logger.warning(
            "assistant provider call failed",
            extra={"org_id": str(context.org_id), "model": model, "error": str(exc)},
        )
        # Nothing is committed on this path: the request's session is discarded without a
        # commit, so the user message and any pending action a tool flushed roll back with it.
        raise HTTPException(
            status_code=502,
            detail=provider_detail(exc, "the assistant's AI provider rejected this request"),
        ) from exc
    finally:
        reset_conversation(token)

    flags = protected_terms(turn.text)
    if flags:
        logger.warning(
            "assistant reply flagged for review",
            extra={"conversation_id": str(conversation.id), "terms": flags},
        )

    for index, message in enumerate(turn.new_messages):
        is_last = index == len(turn.new_messages) - 1
        session.add(
            AssistantMessage(
                id=uuid.uuid4(),
                org_id=context.org_id,
                conversation_id=conversation.id,
                role=message["role"],
                content=message.get("content"),
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                name=message.get("name"),
                prompt_tokens=turn.usage.prompt_tokens if is_last else 0,
                completion_tokens=turn.usage.completion_tokens if is_last else 0,
                fairness_flags=flags if is_last and flags else None,
                created_at=base + timedelta(microseconds=index + 1),
            )
        )

    conversation.title = conversation.title or body.message[:TITLE_CHARS]
    pending = await _pending_for(session, conversation)
    pending_out = await _pending_out(session, pending)
    await session.commit()

    return MessageOut(
        conversation_id=conversation.id,
        text=turn.text,
        steps=turn.steps,
        pending_action=pending_out,
        fairness_flags=flags,
        truncated=turn.truncated,
    )


@router.get("/assistant/messages", response_model=HistoryOut)
async def read_messages(
    conversation_id: UUID | None = Query(default=None),
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> HistoryOut:
    if conversation_id is not None:
        conversation = await session.scalar(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.org_id == context.org_id,
                AssistantConversation.user_id == context.user.id,
            )
        )
    else:
        conversation = await session.scalar(
            select(AssistantConversation)
            .where(
                AssistantConversation.org_id == context.org_id,
                AssistantConversation.user_id == context.user.id,
            )
            .order_by(AssistantConversation.updated_at.desc())
            .limit(1)
        )

    if conversation is None:
        return HistoryOut(conversation_id=None, messages=[])

    rows = await _history(session, conversation)
    steps_by_id: dict[UUID, list[str]] = {}
    pending_steps: list[str] = []
    for row in rows:
        if row.role == "tool" and row.name:
            pending_steps.append(row.name)
        elif row.role == "assistant" and not row.tool_calls:
            steps_by_id[row.id] = pending_steps
            pending_steps = []

    pending = await _pending_for(session, conversation)

    return HistoryOut(
        conversation_id=conversation.id,
        messages=[
            HistoryMessageOut(
                role=row.role,
                content=row.content,
                steps=steps_by_id.get(row.id, []),
                fairness_flags=row.fairness_flags or [],
                created_at=row.created_at,
            )
            for row in rows
            if row.role in ("user", "assistant") and not row.tool_calls
        ],
        pending_action=await _pending_out(session, pending),
    )


@router.post("/assistant/actions/{action_id}/confirm", response_model=ConfirmOut)
async def confirm_action(
    action_id: UUID,
    body: ConfirmRequest,
    context: TenantContext = Depends(require(Permission.CALL_CREATE)),
    session: AsyncSession = Depends(get_session),
) -> ConfirmOut:
    action = await session.scalar(
        select(AssistantPendingAction)
        .join(
            AssistantConversation,
            AssistantConversation.id == AssistantPendingAction.conversation_id,
        )
        .where(
            AssistantPendingAction.id == action_id,
            AssistantPendingAction.org_id == context.org_id,
            AssistantConversation.user_id == context.user.id,
        )
        .with_for_update(of=AssistantPendingAction)
    )
    if action is None:
        raise HTTPException(status_code=404, detail="not found")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"this action is already {action.status}")

    now = datetime.now(timezone.utc)
    if action.created_at < now - ACTION_TTL:
        action.status = "expired"
        action.resolved_at = now
        await session.commit()
        raise HTTPException(status_code=409, detail="this proposal has expired; ask again")

    if not body.confirm:
        action.status = "cancelled"
        action.resolved_at = now
        session.add(
            AuditEvent(
                org_id=context.org_id,
                actor_user_id=context.user.id,
                entity_type="assistant_action",
                entity_id=action.id,
                event_type="assistant_action_cancelled",
                payload={"action": action.action},
            )
        )
        await session.commit()
        return ConfirmOut(action=action.action, status="cancelled")

    ids = [UUID(value) for value in action.payload.get("application_ids", [])]
    applications = (
        await session.scalars(
            select(Application).where(
                Application.org_id == context.org_id, Application.id.in_(ids)
            )
        )
    ).all()
    by_id = {a.id: a for a in applications}

    results: list[dict] = []
    for application_id in ids:
        application = by_id.get(application_id)
        if application is None:
            results.append(
                {
                    "application_id": str(application_id),
                    "workflow_id": None,
                    "status": "not_found",
                }
            )
            continue
        try:
            workflow_id = await start_screening_for(
                session,
                context,
                application,
                {"source": "assistant", "pending_action_id": str(action.id)},
            )
            results.append(
                {
                    "application_id": str(application_id),
                    "workflow_id": workflow_id,
                    "status": "started",
                }
            )
        except HTTPException as exc:
            results.append(
                {
                    "application_id": str(application_id),
                    "workflow_id": None,
                    "status": "already_running" if exc.status_code == 409 else "failed",
                    "detail": str(exc.detail),
                }
            )

    action.status = "confirmed"
    action.resolved_at = now
    started = sum(1 for r in results if r["status"] == "started")
    session.add(
        AuditEvent(
            org_id=context.org_id,
            actor_user_id=context.user.id,
            entity_type="assistant_action",
            entity_id=action.id,
            event_type="assistant_action_confirmed",
            payload={"action": action.action, "results": results},
        )
    )
    session.add(
        AssistantMessage(
            id=uuid.uuid4(),
            org_id=context.org_id,
            conversation_id=action.conversation_id,
            role="assistant",
            created_at=datetime.now(timezone.utc),
            content=(
                f"Started {started} of {len(results)} screening invite(s). "
                "The rest are listed with why they did not start."
            ),
        )
    )
    await session.commit()

    return ConfirmOut(action=action.action, status="confirmed", results=results)


@router.delete("/assistant/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    context: TenantContext = Depends(require(Permission.CANDIDATE_READ)),
    session: AsyncSession = Depends(get_session),
) -> None:
    conversation = await session.scalar(
        select(AssistantConversation).where(
            AssistantConversation.id == conversation_id,
            AssistantConversation.org_id == context.org_id,
            AssistantConversation.user_id == context.user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="not found")

    for model in (AssistantPendingAction, AssistantMessage):
        await session.execute(
            delete(model).where(
                model.conversation_id == conversation.id,
                model.org_id == context.org_id,
            )
        )
    await session.execute(
        delete(AssistantConversation).where(AssistantConversation.id == conversation.id)
    )
    await session.commit()


__all__ = ["router", "PROMPT_VERSION"]
