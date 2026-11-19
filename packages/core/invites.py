from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from core.settings import get_settings

ALGORITHM = "HS256"
PURPOSE = "call_invite"

# How long after expiry a genuine link still resolves, so it can forward a candidate to the
# newer invite for the same application (re-sent invites, callbacks). Never to the old call.
FORWARD_GRACE = timedelta(days=30)


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


@dataclass(frozen=True)
class InviteLink:
    call_id: UUID
    expired: bool


def _decode(token: str, leeway: timedelta = timedelta(0)) -> dict:
    try:
        claims = jwt.decode(
            token,
            _signing_key(),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
            leeway=leeway,
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredInvite("invite has expired") from exc
    except jwt.PyJWTError as exc:
        raise InvalidInvite("invalid invite") from exc

    if claims.get("purpose") != PURPOSE:
        raise InvalidInvite("invalid invite")
    return claims


def _call_id(claims: dict) -> UUID:
    try:
        return UUID(claims["sub"])
    except ValueError as exc:
        raise InvalidInvite("invalid invite") from exc


def verify_invite_token(token: str) -> UUID:
    """The call a still-valid invite token was issued for."""
    return _call_id(_decode(token))


def read_invite_token(token: str) -> InviteLink:
    """Like verify_invite_token, but a genuine token stays readable for FORWARD_GRACE after it
    expires, flagged as expired. Callers may only use an expired link to find the candidate's
    current invite, never to join the call it was issued for."""
    claims = _decode(token, leeway=FORWARD_GRACE)
    expired = claims["exp"] <= datetime.now(timezone.utc).timestamp()
    return InviteLink(call_id=_call_id(claims), expired=expired)


def invite_url(token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/join/{token}"


def is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at <= datetime.now(timezone.utc)
