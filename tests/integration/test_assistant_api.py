import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_read_api import auth_header, client, make_member  # noqa: F401

from core.assistant import AssistantTurn
from core.assistant_tools import TOOLS, reset_conversation, set_conversation
from core.models import (
    Application,
    AssistantConversation,
    AssistantMessage,
    AssistantPendingAction,
    AuditEvent,
    Candidate,
    Resume,
)
from core.schemas import TokenUsage
from core.settings import get_settings


@pytest.fixture(autouse=True)
def llm_configured(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "test-model")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def scripted(monkeypatch):
    """Drive the route without an LLM: run the given tool calls, then answer."""

    def install(tool_calls: list[tuple[str, dict]], text: str = "Here you go."):
        captured: dict = {}

        async def fake_run_turn(client, model, system_prompt, history, tools, execute):
            captured["system_prompt"] = system_prompt
            captured["results"] = [
                await execute(name, json.dumps(args)) for name, args in tool_calls
            ]
            return AssistantTurn(
                text=text,
                steps=[name for name, _ in tool_calls],
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
                new_messages=[{"role": "assistant", "content": text}],
            )

        monkeypatch.setattr("services.api.routes.assistant.run_turn", fake_run_turn)
        return captured

    return install


async def make_applicant(session: AsyncSession, org_id, job_id, name: str, ready: bool = True):
    candidate = Candidate(
        id=uuid.uuid4(),
        org_id=org_id,
        first_name=name,
        last_name="Person",
        email=f"{name.lower()}-{uuid.uuid4().hex}@candidates.test",
    )
    session.add(candidate)
    await session.flush()

    application = Application(
        id=uuid.uuid4(),
        org_id=org_id,
        candidate_id=candidate.id,
        job_id=job_id,
        stage="shortlisted",
    )
    session.add(application)
    if ready:
        session.add(
            Resume(
                id=uuid.uuid4(),
                org_id=org_id,
                candidate_id=candidate.id,
                job_id=job_id,
                pdf_url="s3://test-bucket/r.pdf",
                filename="r.pdf",
                byte_size=10,
                status="ready",
                text="resume text",
                data={"skills": ["Python"]},
            )
        )
    await session.flush()
    return application


async def test_a_viewer_can_chat(client, session, two_tenants, scripted):
    scripted([], "I can help with that.")
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "who is strongest?"},
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    assert response.json()["text"] == "I can help with that."


async def test_a_non_member_gets_404_not_403(client, session, two_tenants, scripted):
    scripted([])
    sub = await make_member(session, two_tenants["org_b"], "owner")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "hello"},
        headers=auth_header(sub),
    )

    assert response.status_code == 404


async def test_a_viewer_is_denied_the_propose_screening_tool(
    client, session, two_tenants, scripted
):
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    captured = scripted(
        [("propose_screening", {"application_ids": [str(application.id)], "reason": "strong"})]
    )
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "screen asha"},
        headers=auth_header(sub),
    )

    assert response.status_code == 200
    assert response.json()["pending_action"] is None
    result = json.loads(captured["results"][0])
    assert result["error"] == "permission_denied"

    pending = await session.scalar(
        select(func.count())
        .select_from(AssistantPendingAction)
        .where(AssistantPendingAction.org_id == two_tenants["org_a"])
    )
    assert pending == 0


async def test_a_recruiter_proposal_writes_a_pending_action_and_starts_nothing(
    client, session, two_tenants, scripted
):
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    scripted(
        [("propose_screening", {"application_ids": [str(application.id)], "reason": "strong"})]
    )
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "screen asha"},
        headers=auth_header(sub),
    )

    body = response.json()
    assert body["pending_action"] is not None
    assert body["pending_action"]["applications"][0]["candidate_name"] == "Asha Person"

    action = await session.scalar(
        select(AssistantPendingAction).where(
            AssistantPendingAction.org_id == two_tenants["org_a"]
        )
    )
    assert action is not None
    assert action.status == "pending"

    proposed = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.org_id == two_tenants["org_a"],
            AuditEvent.event_type == "assistant_action_proposed",
        )
    )
    assert proposed == 1


async def test_a_candidate_without_a_resume_is_not_proposed(
    client, session, two_tenants, scripted
):
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Bare", ready=False
    )
    captured = scripted(
        [("propose_screening", {"application_ids": [str(application.id)], "reason": "x"})]
    )
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "screen them"},
        headers=auth_header(sub),
    )

    result = json.loads(captured["results"][0])
    assert result["status"] == "nothing_to_propose"
    assert result["applications"][0]["reason"] == "no extracted resume yet"


async def test_a_tool_cannot_reach_another_tenants_job(client, session, two_tenants, scripted):
    captured = scripted([("rank_candidates_for_job", {"job_id": str(two_tenants["job_b"])})])
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "rank them"},
        headers=auth_header(sub),
    )

    result = json.loads(captured["results"][0])
    assert result["error"] == "not_found"


async def test_a_tool_cannot_reach_another_tenants_application(
    client, session, two_tenants, scripted
):
    foreign = await make_applicant(
        session, two_tenants["org_b"], two_tenants["job_b"], "Outsider"
    )
    captured = scripted([("get_candidate_profile", {"application_id": str(foreign.id)})])
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "tell me about them"},
        headers=auth_header(sub),
    )

    result = json.loads(captured["results"][0])
    assert result["error"] == "not_found"


async def test_invalid_tool_arguments_become_a_tool_error(client, session, two_tenants, scripted):
    captured = scripted([("rank_candidates_for_job", {"job_id": "not-a-uuid"})])
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "rank"},
        headers=auth_header(sub),
    )

    result = json.loads(captured["results"][0])
    assert result["error"] == "invalid_arguments"


async def test_the_system_prompt_names_the_caller_and_their_role(
    client, session, two_tenants, scripted
):
    captured = scripted([])
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "hello"},
        headers=auth_header(sub),
    )

    assert "recruiter" in captured["system_prompt"]
    assert "You are Kiran" in captured["system_prompt"]


async def test_history_is_returned_for_the_caller_only(client, session, two_tenants, scripted):
    scripted([], "first answer")
    mine = await make_member(session, two_tenants["org_a"], "recruiter")
    theirs = await make_member(session, two_tenants["org_a"], "recruiter")

    await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "my question"},
        headers=auth_header(mine),
    )

    ours = await client.get(
        f"/tenants/{two_tenants['org_a']}/assistant/messages", headers=auth_header(mine)
    )
    others = await client.get(
        f"/tenants/{two_tenants['org_a']}/assistant/messages", headers=auth_header(theirs)
    )

    assert [m["content"] for m in ours.json()["messages"]] == ["my question", "first answer"]
    assert others.json()["messages"] == []


async def test_another_users_conversation_is_404(client, session, two_tenants, scripted):
    scripted([])
    mine = await make_member(session, two_tenants["org_a"], "recruiter")
    theirs = await make_member(session, two_tenants["org_a"], "recruiter")

    first = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "hello"},
        headers=auth_header(mine),
    )
    conversation_id = first.json()["conversation_id"]

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "sneak", "conversation_id": conversation_id},
        headers=auth_header(theirs),
    )

    assert response.status_code == 404


async def test_deleting_a_conversation_removes_its_messages(
    client, session, two_tenants, scripted
):
    scripted([])
    sub = await make_member(session, two_tenants["org_a"], "recruiter")
    first = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "hello"},
        headers=auth_header(sub),
    )
    conversation_id = first.json()["conversation_id"]

    response = await client.delete(
        f"/tenants/{two_tenants['org_a']}/assistant/conversations/{conversation_id}",
        headers=auth_header(sub),
    )

    assert response.status_code == 204
    # Scoped to this tenant: the dev database this runs against holds real conversations.
    left = await session.scalar(
        select(func.count())
        .select_from(AssistantMessage)
        .where(AssistantMessage.org_id == two_tenants["org_a"])
    )
    conversations = await session.scalar(
        select(func.count())
        .select_from(AssistantConversation)
        .where(AssistantConversation.org_id == two_tenants["org_a"])
    )
    assert (left, conversations) == (0, 0)


def test_the_tool_set_has_no_transcript_tool():
    assert "get_call_transcript" not in TOOLS
    assert set(TOOLS) == {
        "list_jobs",
        "rank_candidates_for_job",
        "search_candidates",
        "get_candidate_profile",
        "get_screening_summary",
        "explain_ranking",
        "draft_interview_questions",
        "propose_screening",
    }


async def test_a_provider_failure_is_a_502_with_the_reason(
    client, session, two_tenants, monkeypatch
):
    from openai import APIStatusError

    class Boom(APIStatusError):
        def __init__(self) -> None:
            self.message = "This request requires more credits"
            self.body = {"error": {"message": "This request requires more credits"}}
            Exception.__init__(self, self.message)

    async def explode(*args, **kwargs):
        raise Boom()

    monkeypatch.setattr("services.api.routes.assistant.run_turn", explode)
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    response = await client.post(
        f"/tenants/{two_tenants['org_a']}/assistant/messages",
        json={"message": "hello"},
        headers=auth_header(sub),
    )

    assert response.status_code == 502
    assert "more credits" in response.json()["detail"]
