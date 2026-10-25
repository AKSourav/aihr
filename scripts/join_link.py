import argparse
import asyncio

from sqlalchemy import select

from core.db import async_session
from core.models import Application, Candidate
from core.settings import get_settings
from scripts.start_call import _print_join_link


async def join_link(persona_key: str) -> None:
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

    await _print_join_link(settings, application_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print the join link for a persona's latest dispatched call.")
    parser.add_argument("--persona", required=True)
    args = parser.parse_args()
    asyncio.run(join_link(args.persona))
