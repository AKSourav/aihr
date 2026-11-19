import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from core.settings import get_settings
from services.api import auth

ISSUER = "http://kc.test/realms/aihr"
AUDIENCE = "aihr-api"

signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def keycloak_env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_URL", "http://kc.test")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "")
    monkeypatch.setenv("KEYCLOAK_REALM", "aihr")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()

    class StubSigningKey:
        key = signing_key.public_key()

    class StubClient:
        def get_signing_key_from_jwt(self, token):
            return StubSigningKey()

    monkeypatch.setattr(auth, "jwk_client", lambda: StubClient())
    yield
    get_settings.cache_clear()


def make_token(key=signing_key, algorithm="RS256", **overrides):
    now = int(time.time())
    claims = {
        "sub": str(uuid.uuid4()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "email": "hr@acme.test",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm=algorithm)


def test_a_valid_token_decodes():
    subject = str(uuid.uuid4())

    claims = auth.decode_token(make_token(sub=subject))

    assert claims["sub"] == subject
    assert claims["email"] == "hr@acme.test"


def test_an_expired_token_is_rejected():
    now = int(time.time())

    with pytest.raises(jwt.ExpiredSignatureError):
        auth.decode_token(make_token(exp=now - 120, iat=now - 720))


def test_a_token_for_another_audience_is_rejected():
    with pytest.raises(jwt.InvalidAudienceError):
        auth.decode_token(make_token(aud="some-other-api"))


def test_a_token_from_another_issuer_is_rejected():
    with pytest.raises(jwt.InvalidIssuerError):
        auth.decode_token(make_token(iss="http://evil.test/realms/aihr"))


def test_a_token_signed_by_another_key_is_rejected():
    with pytest.raises(jwt.InvalidSignatureError):
        auth.decode_token(make_token(key=other_key))


def test_a_symmetrically_signed_token_is_rejected():
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 600},
        "a-shared-secret-of-sufficient-length-for-sha256",
        algorithm="HS256",
    )

    with pytest.raises(jwt.InvalidAlgorithmError):
        auth.decode_token(forged)


def test_clock_skew_within_leeway_is_tolerated():
    now = int(time.time())

    claims = auth.decode_token(make_token(exp=now - 10, iat=now - 610))

    assert claims["iss"] == ISSUER


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc", "abc"])
def test_malformed_authorization_headers_are_rejected(header):
    with pytest.raises(HTTPException) as excinfo:
        auth.bearer_token(header)

    assert excinfo.value.status_code == 401


def test_a_well_formed_bearer_header_yields_the_token():
    assert auth.bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


def test_the_issuer_falls_back_to_the_internal_url():
    assert auth.issuer() == ISSUER
    assert auth.realm_url() == ISSUER


def test_a_public_issuer_is_validated_while_keys_stay_internal(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "http://localhost:8080")
    get_settings.cache_clear()

    assert auth.issuer() == "http://localhost:8080/realms/aihr"
    assert auth.realm_url() == ISSUER


def test_a_token_from_the_public_issuer_is_accepted(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "http://localhost:8080")
    get_settings.cache_clear()

    claims = auth.decode_token(make_token(iss="http://localhost:8080/realms/aihr"))

    assert claims["iss"] == "http://localhost:8080/realms/aihr"


def test_a_token_from_the_internal_hostname_is_rejected_once_split(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "http://localhost:8080")
    get_settings.cache_clear()

    with pytest.raises(jwt.InvalidIssuerError):
        auth.decode_token(make_token(iss=ISSUER))
