import importlib
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, Organization, ProviderCredential, User
from core.secrets import decrypt_secret, encrypt_secret, last4
from core.settings import get_settings
from services.api import auth

SECRET = "sk-provider-wxyz"


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DEV_ROUTES_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(monkeypatch, session: AsyncSession):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "tok")
    monkeypatch.setattr(
        auth, "decode_token", lambda token: {"sub": token, "email": f"{token}@acme.test"}
    )
    get_settings.cache_clear()

    import services.api.main as api_main

    app = importlib.reload(api_main).app
    app.dependency_overrides[get_session] = lambda: session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


async def member(session: AsyncSession, org_id, role: str) -> User:
    user = User(id=uuid.uuid4(), keycloak_sub=str(uuid.uuid4()), email=f"{uuid.uuid4()}@acme.test")
    session.add(user)
    await session.flush()
    session.add(Membership(id=uuid.uuid4(), user_id=user.id, org_id=org_id, role=role))
    await session.flush()
    return user


def header(user: User) -> dict:
    return {"Authorization": f"Bearer {user.keycloak_sub}"}


def path(two_tenants, suffix=""):
    return (
        f"/accounts/{two_tenants['account']}/tenants/{two_tenants['org_a']}"
        f"{suffix}/provider-settings"
    )


async def test_an_owner_stores_a_credential_and_never_reads_it_back(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    written = await client.put(
        path(two_tenants),
        headers=header(owner),
        json={
            "selection": {"tts_provider": "elevenlabs", "tts_voice_id": "rachel"},
            "credentials": [{"provider": "elevenlabs", "api_key": SECRET}],
        },
    )
    read = await client.get(path(two_tenants), headers=header(owner))

    assert written.status_code == 200
    assert read.status_code == 200
    body = read.json()
    assert body["selection"]["tts_voice_id"] == "rachel"
    assert body["credentials"][0]["key_last4"] == "wxyz"
    assert SECRET not in written.text
    assert SECRET not in read.text
    assert "api_key" not in body["credentials"][0]


async def test_the_stored_secret_is_encrypted_at_rest(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "api_key": SECRET}]},
    )

    row = await session.scalar(
        select(ProviderCredential).where(ProviderCredential.provider == "deepgram")
    )
    assert row is not None
    assert SECRET not in row.secret_ciphertext
    assert decrypt_secret(row.secret_ciphertext) == SECRET


async def test_omitting_the_api_key_leaves_the_stored_secret_untouched(
    client, session, two_tenants
):
    owner = await member(session, two_tenants["org_a"], "owner")
    await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "api_key": SECRET}]},
    )

    await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "base_url": "https://gw.test"}]},
    )

    row = await session.scalar(
        select(ProviderCredential).where(ProviderCredential.provider == "deepgram")
    )
    assert row is not None
    assert decrypt_secret(row.secret_ciphertext) == SECRET
    assert row.base_url == "https://gw.test"


async def test_an_empty_api_key_deletes_the_credential(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "api_key": SECRET}]},
    )

    await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "api_key": ""}]},
    )

    rows = (await session.scalars(select(ProviderCredential))).all()
    assert rows == []


async def test_a_non_owner_is_refused(client, session, two_tenants):
    admin = await member(session, two_tenants["org_a"], "admin")
    recruiter = await member(session, two_tenants["org_a"], "recruiter")

    assert (await client.get(path(two_tenants), headers=header(admin))).status_code == 403
    assert (await client.get(path(two_tenants), headers=header(recruiter))).status_code == 404


async def test_a_job_scoped_override_is_stored_separately(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "api_key": "org-key-1111"}]},
    )

    job_path = path(two_tenants, f"/jobs/{two_tenants['job_a']}")
    written = await client.put(
        job_path,
        headers=header(owner),
        json={"credentials": [{"provider": "deepgram", "api_key": "job-key-2222"}]},
    )

    assert written.status_code == 200
    assert written.json()["credentials"][0]["key_last4"] == "2222"
    rows = (await session.scalars(select(ProviderCredential))).all()
    assert len(rows) == 2


async def test_a_job_from_another_org_is_not_found(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.get(
        path(two_tenants, f"/jobs/{two_tenants['job_b']}"), headers=header(owner)
    )

    assert response.status_code == 404


async def test_an_unsupported_provider_is_rejected(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "acme-speech", "api_key": SECRET}]},
    )

    assert response.status_code == 422


HOSTILE_URLS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://169.254.169.254/",
    "https://[::1]/v1",
    "https://127.0.0.1/v1",
    "https://localhost/v1",
    "https://keycloak/v1",
    "https://postgres.internal/v1",
    "https://minio.local/v1",
    "file:///etc/passwd",
    "gopher://gw.example.com/",
    "https://user:pass@gw.example.com/v1",
    "https://gw/v1",
]


@pytest.mark.parametrize("url", HOSTILE_URLS)
async def test_a_hostile_credential_base_url_is_rejected(client, session, two_tenants, url):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"credentials": [{"provider": "openai", "api_key": SECRET, "base_url": url}]},
    )

    assert response.status_code == 422
    assert "base_url" in response.text
    assert (await session.scalars(select(ProviderCredential))).all() == []


@pytest.mark.parametrize("url", HOSTILE_URLS)
async def test_a_hostile_llm_base_url_is_rejected(client, session, two_tenants, url):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        path(two_tenants),
        headers=header(owner),
        json={"selection": {"llm_base_url": url}},
    )

    assert response.status_code == 422
    organization = await session.get(Organization, two_tenants["org_a"])
    assert organization is not None
    assert organization.provider_config in (None, {})


async def test_a_normal_gateway_url_is_accepted(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        path(two_tenants),
        headers=header(owner),
        json={
            "selection": {"llm_base_url": "https://api.openai.com/v1"},
            "credentials": [
                {
                    "provider": "openai",
                    "api_key": SECRET,
                    "base_url": "https://gateway.example.com:8443/v1",
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["selection"]["llm_base_url"] == "https://api.openai.com/v1"
    assert body["credentials"][0]["base_url"] == "https://gateway.example.com:8443/v1"


async def test_plain_http_is_accepted_only_when_dev_routes_are_enabled(
    client, session, monkeypatch, two_tenants
):
    owner = await member(session, two_tenants["org_a"], "owner")
    body = {"selection": {"llm_base_url": "http://gateway.example.com/v1"}}

    refused = await client.put(path(two_tenants), headers=header(owner), json=body)

    monkeypatch.setenv("DEV_ROUTES_ENABLED", "true")
    get_settings.cache_clear()
    allowed = await client.put(path(two_tenants), headers=header(owner), json=body)

    assert refused.status_code == 422
    assert allowed.status_code == 200


async def test_a_stored_value_is_not_revalidated_on_read(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    organization = await session.get(Organization, two_tenants["org_a"])
    assert organization is not None
    organization.provider_config = {"llm_base_url": "http://169.254.169.254/"}
    session.add(
        ProviderCredential(
            org_id=two_tenants["org_a"],
            job_id=None,
            provider="openai",
            base_url="http://keycloak:8080",
            secret_ciphertext=encrypt_secret(SECRET),
            secret_last4=last4(SECRET),
        )
    )
    await session.flush()

    response = await client.get(path(two_tenants), headers=header(owner))

    assert response.status_code == 200
    body = response.json()
    assert body["selection"]["llm_base_url"] == "http://169.254.169.254/"
    assert body["credentials"][0]["base_url"] == "http://keycloak:8080"
