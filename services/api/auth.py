import logging
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import User
from core.settings import get_settings

logger = logging.getLogger(__name__)

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 30


def issuer() -> str:
    settings = get_settings()
    return f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"


@lru_cache
def jwk_client() -> PyJWKClient:
    return PyJWKClient(f"{issuer()}/protocol/openid-connect/certs")


def decode_token(token: str) -> dict:
    signing_key = jwk_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=ALGORITHMS,
        audience=get_settings().keycloak_audience,
        issuer=issuer(),
        leeway=LEEWAY_SECONDS,
    )


def bearer_token(authorization: str = Header(default="")) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token.strip()


async def current_user(
    token: str = Depends(bearer_token), session: AsyncSession = Depends(get_session)
) -> User:
    try:
        claims = decode_token(token)
    except Exception as exc:
        logger.warning("token rejected", extra={"error": str(exc)})
        raise HTTPException(status_code=401, detail="invalid token") from exc

    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="invalid token")

    user = await session.scalar(select(User).where(User.keycloak_sub == subject))
    if user is None:
        user = User(keycloak_sub=subject, email=claims.get("email", ""))
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info("provisioned user", extra={"keycloak_sub": subject})

    return user
