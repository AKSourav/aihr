import importlib
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Membership, ProviderCredential, User
from core.secrets import decrypt_secret
from core.settings import get_settings
from services.api import auth

SECRET = "sk-provider-wxyz"


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
