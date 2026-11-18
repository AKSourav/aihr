from datetime import datetime, timezone
from uuid import UUID

import jwt

from core.settings import get_settings

ALGORITHM = "HS256"
PURPOSE = "call_invite"


class InvalidInvite(Exception):
    pass


class ExpiredInvite(InvalidInvite):
    pass


def _signing_key() -> str:
    key = get_settings().invite_signing_key
    if len(key) < 32:
        raise RuntimeError("INVITE_SIGNING_KEY must be set to at least 32 characters")
    return key


def issue_invite_token(call_id: UUID, expires_at: datetime) -> str:
    claims = {"sub": str(call_id), "purpose": PURPOSE, "exp": int(expires_at.timestamp())}
    return jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)


def verify_invite_token(token: str) -> UUID:
    try:
        claims = jwt.decode(
            token, _signing_key(), algorithms=[ALGORITHM], options={"require": ["sub", "exp"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredInvite("invite has expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidInvite("invalid invite") from exc

    if claims.get("purpose") != PURPOSE:
        raise InvalidInvite("invalid invite")
    try:
        return UUID(claims["sub"])
    except ValueError as exc:
        raise InvalidInvite("invalid invite") from exc


def invite_url(token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/join/{token}"


def is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at <= datetime.now(timezone.utc)
