import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import async_session
from core.models import Membership, Organization, User


async def upsert_user(session: AsyncSession, sub: str, email: str, platform_admin: bool) -> User:
    user = await session.scalar(select(User).where(User.keycloak_sub == sub))
    if user is None:
        user = User(keycloak_sub=sub, email=email)
        session.add(user)
    user.email = email
    user.is_platform_admin = platform_admin
    await session.flush()
    return user


async def grant(sub: str, email: str, org_name: str | None, role: str | None, platform_admin: bool) -> None:
    async with async_session() as session:
        user = await upsert_user(session, sub, email, platform_admin)

        if org_name and role:
            org = await session.scalar(select(Organization).where(Organization.name == org_name))
            if org is None:
                raise SystemExit(f"no organization named {org_name!r}")

            membership = await session.scalar(
                select(Membership).where(
                    Membership.user_id == user.id, Membership.org_id == org.id
                )
            )
            if membership is None:
                session.add(Membership(user_id=user.id, org_id=org.id, role=role))
            else:
                membership.role = role

        await session.commit()

    scope = f"{role} on {org_name}" if org_name and role else "no tenant membership"
    print(f"{email} ({sub}): {scope}, platform_admin={platform_admin}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Grant a Keycloak subject access to a tenant.")
    parser.add_argument("--sub", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--org")
    parser.add_argument("--role", choices=["admin", "recruiter", "viewer"])
    parser.add_argument("--platform-admin", action="store_true")
    args = parser.parse_args()

    asyncio.run(grant(args.sub, args.email, args.org, args.role, args.platform_admin))


if __name__ == "__main__":
    main()
