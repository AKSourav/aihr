import argparse
import asyncio
import urllib.parse

from livekit import api
from sqlalchemy import select
from temporalio.client import Client

from core.db import async_session
from core.models import Application, Candidate, Call
from core.settings import get_settings
from services.temporal_worker.shared import ScreeningCallInput

JOIN_LINK_POLL_TIMEOUT_S = 30.0


async def _print_join_link(settings, application_id) -> None:
    deadline = asyncio.get_event_loop().time() + JOIN_LINK_POLL_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        async with async_session() as session:
            call = await session.scalar(
                select(Call)
                .where(Call.application_id == application_id, Call.room_name.is_not(None))
                .order_by(Call.attempt_no.desc())
            )
        if call is not None:
            token = (
                api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
                .with_identity("candidate")
                .with_name("Candidate")
                .with_grants(api.VideoGrants(room_join=True, room=str(call.room_name)))
                .to_jwt()
            )
            test_page_url = "http://localhost:8080/join.html?" + urllib.parse.urlencode(
                {"url": settings.livekit_url, "token": token}
            )
            print(f"Room: {call.room_name}")
            print(f"Test page (run `make webtest` first): {test_page_url}")
            return
        await asyncio.sleep(1.0)
    print("Timed out waiting for the room to be dispatched; check `make logs-agent`.")


async def start_call(persona_key: str, mode: str, skip_business_hours: bool, fast_retries: bool) -> None:
    settings = get_settings()
    email = f"{persona_key}@candidates.test"

    async with async_session() as session:
        candidate = await session.scalar(select(Candidate).where(Candidate.email == email))
        if candidate is None:
            raise SystemExit(f"no seeded candidate for key '{persona_key}'")

        application = await session.scalar(
            select(Application).where(Application.candidate_id == candidate.id).order_by(Application.created_at)
        )
        if application is None:
            raise SystemExit(f"candidate '{persona_key}' has no application")

        application_id = application.id
        job_id = application.job_id
        candidate_id = application.candidate_id

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    workflow_id = f"call-{job_id}-{candidate_id}"
    handle = await client.start_workflow(
        "ScreeningCallWorkflow",
        ScreeningCallInput(
            application_id=str(application_id),
            call_mode=mode,
            skip_business_hours=skip_business_hours,
            fast_retries=fast_retries,
        ),
        id=workflow_id,
        task_queue="voice",
    )

    print(f"Started workflow: {workflow_id}")
    print(f"Temporal UI: http://localhost:8233/namespaces/{settings.temporal_namespace}/workflows/{workflow_id}/{handle.first_execution_run_id}")

    if mode == "web":
        await _print_join_link(settings, application_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start a ScreeningCallWorkflow for a seeded candidate.")
    parser.add_argument("--persona", required=True)
    parser.add_argument("--mode", default="web", choices=["web", "sim", "sip", "console"])
    parser.add_argument("--skip-business-hours", action="store_true")
    parser.add_argument("--fast-retries", action="store_true")
    args = parser.parse_args()
    asyncio.run(start_call(args.persona, args.mode, args.skip_business_hours, args.fast_retries))
