"""Self-service signup.

Unauthenticated: it is the only way to become a customer. It creates the Keycloak identity,
the account, the organization and an owner membership, then asks Keycloak to email a
set-password link. The row existing grants nothing until that password is set.

An address that is already known always returns the same 202 as a fresh one, so the endpoint
cannot be used to discover who has an account.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import User
from services.api import keycloak_admin
from services.api.routes.organizations import found_organization

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public"])


class SignupRequest(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    organization_name: str = Field(min_length=1, max_length=200)


class SignupResponse(BaseModel):
    status: str


@router.post("/signup", response_model=SignupResponse, status_code=202)
async def signup(
    body: SignupRequest, session: AsyncSession = Depends(get_session)
) -> SignupResponse:
    email = body.email.lower()
    accepted = SignupResponse(status="verification_sent")

    if await session.scalar(select(User).where(User.email == email)) is not None:
        return accepted

    try:
        async with httpx.AsyncClient(timeout=keycloak_admin.TIMEOUT) as client:
            identity = await keycloak_admin.find_user_by_email(client, email)
            if identity is not None:
                return accepted
            subject = await keycloak_admin.create_user(client, email)
            await keycloak_admin.send_invitation_email(client, subject)
    except keycloak_admin.KeycloakError as exc:
        raise HTTPException(status_code=502, detail="identity provider unavailable") from exc

    user = User(keycloak_sub=subject, email=email)
    session.add(user)
    await session.flush()
    await found_organization(session, user, body.organization_name)
    await session.commit()

    logger.info("signed up", extra={"keycloak_sub": subject})
    return accepted
