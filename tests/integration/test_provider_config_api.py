import importlib
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.models import Account, AccountOwner, Job, Membership, Organization, Secret, User
from core.secrets import decrypt_secret
from core.settings import get_settings
from services.api import auth

SECRET = "sk-provider-wxyz"

HOSTILE_URLS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1/v1",
    "https://localhost/v1",
    "https://keycloak/v1",
    "https://0x7f.0.0.1/v1",
    "file:///etc/passwd",
    "https://user:pass@gw.example.com/v1",
]


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
    if role == "owner":
        organization = await session.get(Organization, org_id)
        assert organization is not None
        session.add(
            AccountOwner(id=uuid.uuid4(), account_id=organization.account_id, user_id=user.id)
        )
    await session.flush()
    return user


def header(user: User) -> dict:
    return {"Authorization": f"Bearer {user.keycloak_sub}"}


def base(two_tenants) -> str:
    return f"/accounts/{two_tenants['account']}"


def tenant(two_tenants) -> str:
    return f"/accounts/{two_tenants['account']}/tenants/{two_tenants['org_a']}"


def account_secrets(two_tenants):
    return select(Secret).where(Secret.account_id == two_tenants["account"])


async def make_secret(client, two_tenants, owner, name: str, value: str = SECRET):
    return await client.post(
        f"{base(two_tenants)}/secrets",
        headers=header(owner),
        json={"name": name, "value": value},
    )


async def test_a_secret_is_stored_encrypted_and_never_returned(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    created = await make_secret(client, two_tenants, owner, "OpenAI production")
    listed = await client.get(f"{base(two_tenants)}/secrets", headers=header(owner))

    assert created.status_code == 201
    assert listed.status_code == 200
    assert SECRET not in created.text
    assert SECRET not in listed.text
    assert created.json()["last4"] == "wxyz"
    assert "value" not in created.json()

    row = await session.scalar(select(Secret).where(Secret.name == "OpenAI production"))
    assert row is not None
    assert SECRET not in row.ciphertext
    assert decrypt_secret(row.ciphertext) == SECRET


async def test_two_secrets_for_the_same_provider_coexist(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    first = await make_secret(client, two_tenants, owner, "OpenAI production", "prod-1111")
    second = await make_secret(client, two_tenants, owner, "Northwind OpenAI", "client-2222")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["secret_id"] != second.json()["secret_id"]
    assert len((await session.scalars(account_secrets(two_tenants))).all()) == 2


async def test_a_duplicate_name_is_refused(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    await make_secret(client, two_tenants, owner, "Deepgram")

    again = await make_secret(client, two_tenants, owner, "Deepgram")

    assert again.status_code == 409


async def test_used_by_names_where_a_secret_is_referenced(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    secret_id = (await make_secret(client, two_tenants, owner, "OpenAI production")).json()[
        "secret_id"
    ]

    await client.put(
        f"{base(two_tenants)}/provider-settings",
        headers=header(owner),
        json={"selection": {"llm_call": {"provider": "openai", "secret_id": secret_id}}},
    )
    listed = await client.get(f"{base(two_tenants)}/secrets", headers=header(owner))

    assert listed.json()[0]["used_by"] == ["Conversation"]


async def test_a_referenced_secret_cannot_be_deleted(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    secret_id = (await make_secret(client, two_tenants, owner, "OpenAI production")).json()[
        "secret_id"
    ]
    await client.put(
        f"{base(two_tenants)}/provider-settings",
        headers=header(owner),
        json={"selection": {"llm_call": {"provider": "openai", "secret_id": secret_id}}},
    )

    refused = await client.delete(f"{base(two_tenants)}/secrets/{secret_id}", headers=header(owner))

    assert refused.status_code == 409
    assert "Conversation" in refused.text
    assert (await session.scalars(account_secrets(two_tenants))).all() != []


async def test_an_unreferenced_secret_can_be_deleted(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    secret_id = (await make_secret(client, two_tenants, owner, "Unused")).json()["secret_id"]

    removed = await client.delete(f"{base(two_tenants)}/secrets/{secret_id}", headers=header(owner))

    assert removed.status_code == 204
    assert (await session.scalars(account_secrets(two_tenants))).all() == []


async def test_replacing_a_value_keeps_the_name_and_changes_last4(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    secret_id = (await make_secret(client, two_tenants, owner, "Deepgram")).json()["secret_id"]

    updated = await client.put(
        f"{base(two_tenants)}/secrets/{secret_id}",
        headers=header(owner),
        json={"value": "rotated-8899"},
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "Deepgram"
    assert updated.json()["last4"] == "8899"
    row = await session.scalar(select(Secret).where(Secret.id == uuid.UUID(secret_id)))
    assert row is not None
    assert decrypt_secret(row.ciphertext) == "rotated-8899"


async def test_a_job_override_is_stored_against_the_job(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    secret_id = (await make_secret(client, two_tenants, owner, "ElevenLabs")).json()["secret_id"]

    written = await client.put(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/provider-settings",
        headers=header(owner),
        json={
            "selection": {
                "tts": {"provider": "elevenlabs", "secret_id": secret_id, "voice_id": "Adam"}
            }
        },
    )

    assert written.status_code == 200
    assert written.json()["selection"]["tts"]["voice_id"] == "Adam"
    job = await session.get(Job, two_tenants["job_a"])
    assert job is not None
    assert job.provider_config["tts"]["voice_id"] == "Adam"
    organization = await session.get(Organization, two_tenants["org_a"])
    assert organization is not None
    assert organization.provider_config in (None, {})


async def test_a_job_read_reports_what_it_would_inherit(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    await client.put(
        f"{tenant(two_tenants)}/provider-settings",
        headers=header(owner),
        json={"selection": {"tts": {"provider": "elevenlabs", "voice_id": "Rachel"}}},
    )

    read = await client.get(
        f"/tenants/{two_tenants['org_a']}/jobs/{two_tenants['job_a']}/provider-settings",
        headers=header(owner),
    )

    assert read.json()["inherited"]["tts"]["voice_id"] == "Rachel"
    assert read.json()["selection"]["tts"]["voice_id"] is None


async def test_an_organization_read_reports_what_it_would_inherit(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    await client.put(
        f"{base(two_tenants)}/provider-settings",
        headers=header(owner),
        json={"selection": {"tts": {"provider": "cartesia", "voice_id": "British Lady"}}},
    )

    read = await client.get(f"{tenant(two_tenants)}/provider-settings", headers=header(owner))

    assert read.json()["inherited"]["tts"]["voice_id"] == "British Lady"
    assert read.json()["selection"]["tts"]["voice_id"] is None


async def test_a_secret_from_another_org_cannot_be_referenced(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")
    outsider = Secret(
        id=uuid.uuid4(),
        account_id=two_tenants["other_account"],
        name="Theirs",
        ciphertext="x",
        last4="zzzz",
    )
    session.add(outsider)
    await session.flush()

    response = await client.put(
        f"{base(two_tenants)}/provider-settings",
        headers=header(owner),
        json={"selection": {"stt": {"provider": "deepgram", "secret_id": str(outsider.id)}}},
    )

    assert response.status_code == 404


async def test_a_non_owner_is_refused(client, session, two_tenants):
    admin = await member(session, two_tenants["org_a"], "admin")
    recruiter = await member(session, two_tenants["org_a"], "recruiter")

    assert (
        await client.get(f"{base(two_tenants)}/secrets", headers=header(admin))
    ).status_code == 403
    assert (
        await client.get(f"{base(two_tenants)}/secrets", headers=header(recruiter))
    ).status_code == 404


@pytest.mark.parametrize("url", HOSTILE_URLS)
async def test_a_hostile_base_url_is_rejected(client, session, two_tenants, url):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        f"{base(two_tenants)}/provider-settings",
        headers=header(owner),
        json={"selection": {"llm_call": {"provider": "openai", "base_url": url}}},
    )

    assert response.status_code == 422


async def test_a_normal_gateway_url_is_accepted(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.put(
        f"{base(two_tenants)}/provider-settings",
        headers=header(owner),
        json={
            "selection": {
                "llm_call": {"provider": "openai", "base_url": "https://openrouter.ai/api/v1"}
            }
        },
    )

    assert response.status_code == 200


async def test_the_catalogue_is_served(client, session, two_tenants):
    owner = await member(session, two_tenants["org_a"], "owner")

    response = await client.get("/provider-catalogue", headers=header(owner))

    assert response.status_code == 200
    assert set(response.json()) == {"stt", "tts", "llm_call", "llm_summary"}
    assert "nova-3" in response.json()["stt"]["deepgram"]["options"]


async def test_an_account_with_no_organizations_is_still_administrable(client, session, two_tenants):
    user = User(id=uuid.uuid4(), keycloak_sub=str(uuid.uuid4()), email=f"{uuid.uuid4()}@acme.test")
    session.add(user)
    await session.flush()

    account = Account(id=uuid.uuid4(), name=f"Empty {uuid.uuid4()}")
    session.add(account)
    await session.flush()
    session.add(AccountOwner(id=uuid.uuid4(), account_id=account.id, user_id=user.id))
    await session.flush()

    created = await client.post(
        f"/accounts/{account.id}/secrets",
        headers=header(user),
        json={"name": "Deepgram", "value": SECRET},
    )
    listed = await client.get(f"/accounts/{account.id}/secrets", headers=header(user))

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()[0]["used_by"] == []


async def test_an_owner_may_create_a_tenant_in_an_empty_account(client, session, two_tenants):
    user = User(id=uuid.uuid4(), keycloak_sub=str(uuid.uuid4()), email=f"{uuid.uuid4()}@acme.test")
    session.add(user)
    await session.flush()

    account = Account(id=uuid.uuid4(), name=f"Empty {uuid.uuid4()}")
    session.add(account)
    await session.flush()
    session.add(AccountOwner(id=uuid.uuid4(), account_id=account.id, user_id=user.id))
    await session.flush()

    response = await client.post(
        f"/accounts/{account.id}/tenants", headers=header(user), json={"name": "First Tenant"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "First Tenant"
