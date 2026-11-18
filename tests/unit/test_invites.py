import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from core.invites import (
    ExpiredInvite,
    InvalidInvite,
    invite_url,
    is_expired,
    issue_invite_token,
    verify_invite_token,
)
from core.settings import get_settings

KEY = "k" * 32


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("INVITE_SIGNING_KEY", KEY)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://hire.example.com/")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def in_hours(hours: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def test_a_token_round_trips_to_its_call():
    call_id = uuid.uuid4()

    assert verify_invite_token(issue_invite_token(call_id, in_hours(1))) == call_id


def test_an_expired_token_is_rejected_as_expired():
    token = issue_invite_token(uuid.uuid4(), in_hours(-1))

    with pytest.raises(ExpiredInvite):
        verify_invite_token(token)


def test_a_tampered_token_is_rejected():
    token = issue_invite_token(uuid.uuid4(), in_hours(1))
    tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")

    with pytest.raises(InvalidInvite):
        verify_invite_token(tampered)


def test_a_token_signed_with_another_key_is_rejected():
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "purpose": "call_invite", "exp": int(in_hours(1).timestamp())},
        "x" * 32,
        algorithm="HS256",
    )

    with pytest.raises(InvalidInvite):
        verify_invite_token(token)


def test_a_token_minted_for_another_purpose_is_rejected():
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "purpose": "password_reset", "exp": int(in_hours(1).timestamp())},
        KEY,
        algorithm="HS256",
    )

    with pytest.raises(InvalidInvite):
        verify_invite_token(token)


def test_a_short_signing_key_is_refused(monkeypatch):
    monkeypatch.setenv("INVITE_SIGNING_KEY", "too-short")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError):
        issue_invite_token(uuid.uuid4(), in_hours(1))


def test_invite_url_points_at_the_join_page():
    assert invite_url("abc") == "https://hire.example.com/join/abc"


def test_is_expired():
    assert is_expired(in_hours(-1))
    assert not is_expired(in_hours(1))
    assert not is_expired(None)
