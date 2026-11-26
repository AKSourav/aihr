"""The MCP endpoint, exercised over real HTTP with the app's lifespan running."""

import importlib
import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_read_api import auth_header, make_member  # noqa: F401

from core.assistant_tools import TOOLS
from core.db import get_session
from core.models import Application, AssistantPendingAction, Candidate, Resume
from core.settings import get_settings
from services.api import auth

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def mcp_client(monkeypatch, session: AsyncSession):
    """A factory, not an async fixture.

    The MCP session manager owns an anyio task group, and anyio requires it to be entered and
    exited in the same task. pytest-asyncio finalises async-generator fixtures in a different
    task than it starts them, so the test itself must own the `async with`.
    """
    monkeypatch.setenv("INTERNAL_API_TOKEN", "tok")
    monkeypatch.setattr(
        auth, "decode_token", lambda token: {"sub": token, "email": f"{token}@acme.test"}
    )
    get_settings.cache_clear()

    import services.api.main as api_main

    app = importlib.reload(api_main).app
    app.dependency_overrides[get_session] = lambda: session

    @asynccontextmanager
    async def connect():
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        ):
            yield c

    yield connect
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def result_of(response: httpx.Response) -> dict:
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def tool_payload(response: httpx.Response) -> dict:
    content = result_of(response)["content"]
    return json.loads(content[0]["text"])


async def call(client, org_id, sub, method, params=None):
    return await client.post(
        f"/tenants/{org_id}/mcp",
        json=rpc(method, params),
        headers={**auth_header(sub), **MCP_HEADERS},
    )


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
            )
        )
    await session.flush()
    return application


async def test_mcp_without_a_bearer_token_is_401(mcp_client, two_tenants):
    async with mcp_client() as client:
        response = await client.post(
            f"/tenants/{two_tenants['org_a']}/mcp",
            json=rpc("tools/list"),
            headers=MCP_HEADERS,
        )

    assert response.status_code == 401


async def test_mcp_for_a_non_member_is_404(mcp_client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_b"], "owner")

    async with mcp_client() as client:
        response = await call(client, two_tenants["org_a"], sub, "tools/list")

    assert response.status_code == 404


async def test_tools_list_returns_the_registry(mcp_client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    async with mcp_client() as client:
        response = await call(client, two_tenants["org_a"], sub, "tools/list")

    assert response.status_code == 200
    names = {tool["name"] for tool in result_of(response)["tools"]}
    assert names == set(TOOLS)


async def test_no_tool_schema_exposes_org_id(mcp_client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    async with mcp_client() as client:
        response = await call(client, two_tenants["org_a"], sub, "tools/list")

    for tool in result_of(response)["tools"]:
        schema = tool.get("inputSchema") or tool.get("input_schema")
        assert "org_id" not in (schema or {}).get("properties", {})


async def test_a_tool_call_runs_under_the_callers_org(mcp_client, session, two_tenants):
    await make_applicant(session, two_tenants["org_a"], two_tenants["job_a"], "Asha")
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    async with mcp_client() as client:
        response = await call(
        client,
        two_tenants["org_a"],
        sub,
        "tools/call",
        {"name": "list_jobs", "arguments": {}},
    )

    assert response.status_code == 200
    payload = tool_payload(response)
    titles = {job["title"] for job in payload["jobs"]}
    assert titles == {"A job"}


async def test_a_tool_cannot_reach_another_tenants_job(mcp_client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    async with mcp_client() as client:
        response = await call(
        client,
        two_tenants["org_a"],
        sub,
        "tools/call",
        {
            "name": "rank_candidates_for_job",
            "arguments": {"job_id": str(two_tenants["job_b"])},
        },
    )

    assert tool_payload(response)["error"] == "not_found"


async def test_a_viewer_is_denied_a_write_tool(mcp_client, session, two_tenants):
    application = await make_applicant(
        session, two_tenants["org_a"], two_tenants["job_a"], "Asha"
    )
    sub = await make_member(session, two_tenants["org_a"], "viewer")

    async with mcp_client() as client:
        response = await call(
        client,
        two_tenants["org_a"],
        sub,
        "tools/call",
        {
            "name": "propose_screening",
            "arguments": {"application_ids": [str(application.id)], "reason": "strong"},
        },
    )

    assert response.status_code == 200
    payload = tool_payload(response)
    assert payload["error"] == "permission_denied"
    assert payload["permission"] == "call:create"

    pending = await session.scalar(
        select(func.count())
        .select_from(AssistantPendingAction)
        .where(AssistantPendingAction.org_id == two_tenants["org_a"])
    )
    assert pending == 0


async def test_an_unknown_tool_is_reported_not_raised(mcp_client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    async with mcp_client() as client:
        response = await call(
        client,
        two_tenants["org_a"],
        sub,
        "tools/call",
        {"name": "drop_database", "arguments": {}},
    )

    assert response.status_code == 200
    assert tool_payload(response)["error"] == "unknown_tool"


async def test_invalid_tool_arguments_are_reported(mcp_client, session, two_tenants):
    sub = await make_member(session, two_tenants["org_a"], "recruiter")

    async with mcp_client() as client:
        response = await call(
        client,
        two_tenants["org_a"],
        sub,
        "tools/call",
        {"name": "rank_candidates_for_job", "arguments": {"job_id": "nope"}},
    )

    assert tool_payload(response)["error"] == "invalid_arguments"
