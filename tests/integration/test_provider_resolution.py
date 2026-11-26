import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Account, Job, Organization, Secret
from core.providers import ProviderNotConfigured, resolve_providers
from core.secrets import SecretDecryptionError, encrypt_secret, last4
from core.settings import get_settings

ENV_KEY = "env-key-cccc"


@pytest.fixture(autouse=True)
def provider_env(monkeypatch):
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DEEPGRAM_API_KEY", ENV_KEY)
    monkeypatch.setenv("TTS_API_KEY", ENV_KEY)
    monkeypatch.setenv("LLM_API_KEY", ENV_KEY)
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "env-summary-model")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def fixtures(
    session: AsyncSession, two_tenants, job_config=None, org_config=None
) -> tuple[Organization, Job]:
    organization = await session.get(Organization, two_tenants["org_a"])
    job = await session.get(Job, two_tenants["job_a"])
    assert organization is not None
    assert job is not None
    organization.provider_config = org_config
    job.provider_config = job_config
    await session.flush()
    return organization, job


async def secret(session: AsyncSession, account_id, name: str, value: str) -> Secret:
    row = Secret(
        id=uuid.uuid4(),
        account_id=account_id,
        name=name,
        ciphertext=encrypt_secret(value),
        last4=last4(value),
    )
    session.add(row)
    await session.flush()
    return row


async def test_the_platform_env_key_is_used_when_no_secret_is_referenced(session, two_tenants):
    organization, job = await fixtures(session, two_tenants)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.stt.provider == "deepgram"
    assert resolved.stt.api_key == ENV_KEY
    assert resolved.llm_summary.model == "env-summary-model"


async def test_a_referenced_secret_beats_the_env_key(session, two_tenants):
    organization, _ = await fixtures(session, two_tenants)
    row = await secret(session, organization.account_id, "Deepgram", "org-key-aaaa")
    organization, job = await fixtures(
        session, two_tenants, org_config={"stt": {"provider": "deepgram", "secret_id": str(row.id)}}
    )

    resolved = await resolve_providers(session, organization, job)

    assert resolved.stt.api_key == "org-key-aaaa"


async def test_a_job_may_point_at_a_different_secret_for_the_same_provider(session, two_tenants):
    organization, _ = await fixtures(session, two_tenants)
    ours = await secret(session, organization.account_id, "OpenAI production", "prod-key-1111")
    theirs = await secret(session, organization.account_id, "Northwind OpenAI", "client-key-2222")

    organization, job = await fixtures(
        session,
        two_tenants,
        org_config={"llm_call": {"provider": "openai", "secret_id": str(ours.id)}},
        job_config={"llm_call": {"secret_id": str(theirs.id)}},
    )

    resolved = await resolve_providers(session, organization, job)

    assert resolved.llm_call.api_key == "client-key-2222"


async def test_one_secret_can_serve_several_slots(session, two_tenants):
    organization, _ = await fixtures(session, two_tenants)
    row = await secret(session, organization.account_id, "OpenAI production", "shared-key-3333")

    organization, job = await fixtures(
        session,
        two_tenants,
        org_config={
            "llm_call": {"provider": "openai", "secret_id": str(row.id)},
            "llm_summary": {"provider": "openai", "secret_id": str(row.id)},
        },
    )

    resolved = await resolve_providers(session, organization, job)

    assert resolved.llm_call.api_key == "shared-key-3333"
    assert resolved.llm_summary.api_key == "shared-key-3333"


async def test_a_job_overriding_a_model_keeps_the_org_voice_and_secret(session, two_tenants):
    organization, _ = await fixtures(session, two_tenants)
    row = await secret(session, organization.account_id, "ElevenLabs", "el-key-4444")

    organization, job = await fixtures(
        session,
        two_tenants,
        org_config={
            "tts": {"provider": "elevenlabs", "secret_id": str(row.id), "voice_id": "Rachel"},
            "llm_call": {"model": "gpt-4o-mini"},
        },
        job_config={"llm_call": {"model": "gpt-4o"}},
    )

    resolved = await resolve_providers(session, organization, job)

    assert resolved.llm_call.model == "gpt-4o"
    assert resolved.tts.voice_id == "Rachel"
    assert resolved.tts.api_key == "el-key-4444"


async def test_elevenlabs_without_a_secret_fails_closed(session, two_tenants):
    organization, job = await fixtures(
        session, two_tenants, job_config={"tts": {"provider": "elevenlabs"}}
    )

    with pytest.raises(ProviderNotConfigured):
        await resolve_providers(session, organization, job)


async def test_a_reference_to_a_deleted_secret_fails_closed(session, two_tenants):
    organization, job = await fixtures(
        session,
        two_tenants,
        org_config={"stt": {"provider": "deepgram", "secret_id": str(uuid.uuid4())}},
    )

    with pytest.raises(ProviderNotConfigured):
        await resolve_providers(session, organization, job)


async def test_an_undecryptable_secret_fails_closed(session, two_tenants, monkeypatch):
    organization, _ = await fixtures(session, two_tenants)
    row = await secret(session, organization.account_id, "Deepgram", "org-key-aaaa")
    organization, job = await fixtures(
        session, two_tenants, org_config={"stt": {"provider": "deepgram", "secret_id": str(row.id)}}
    )

    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    with pytest.raises(SecretDecryptionError):
        await resolve_providers(session, organization, job)


async def test_an_unsupported_provider_is_rejected(session, two_tenants):
    organization, job = await fixtures(
        session, two_tenants, job_config={"tts": {"provider": "nope"}}
    )

    with pytest.raises(ProviderNotConfigured):
        await resolve_providers(session, organization, job)


async def test_another_accounts_secret_is_not_reachable(session, two_tenants):
    organization, _ = await fixtures(session, two_tenants)
    outsider = await secret(session, two_tenants["other_account"], "Deepgram", "other-key-5555")
    organization, job = await fixtures(
        session,
        two_tenants,
        org_config={"stt": {"provider": "deepgram", "secret_id": str(outsider.id)}},
    )

    with pytest.raises(ProviderNotConfigured):
        await resolve_providers(session, organization, job)


async def test_one_account_secret_serves_jobs_in_different_organizations(session, two_tenants):
    organization_a = await session.get(Organization, two_tenants["org_a"])
    organization_b = await session.get(Organization, two_tenants["org_b"])
    assert organization_a is not None and organization_b is not None

    row = await secret(session, organization_a.account_id, "Shared", "shared-key-7777")
    account = await session.get(Account, organization_a.account_id)
    assert account is not None
    account.provider_config = {"stt": {"provider": "deepgram", "secret_id": str(row.id)}}

    job_a = await session.get(Job, two_tenants["job_a"])
    job_b = await session.get(Job, two_tenants["job_b"])
    assert job_a is not None and job_b is not None
    job_a.provider_config = None
    job_b.provider_config = None
    await session.flush()

    in_a = await resolve_providers(session, organization_a, job_a)
    in_b = await resolve_providers(session, organization_b, job_b)

    assert in_a.stt.api_key == "shared-key-7777"
    assert in_b.stt.api_key == "shared-key-7777"


async def test_job_beats_organization_beats_account(session, two_tenants):
    organization = await session.get(Organization, two_tenants["org_a"])
    assert organization is not None
    account = await session.get(Account, organization.account_id)
    assert account is not None

    account.provider_config = {
        "tts": {"provider": "cartesia", "voice_id": "account-voice"},
        "llm_call": {"model": "account-model"},
        "stt": {"model": "account-stt"},
    }
    organization.provider_config = {
        "tts": {"voice_id": "org-voice"},
        "llm_call": {"model": "org-model"},
    }
    job = await session.get(Job, two_tenants["job_a"])
    assert job is not None
    job.provider_config = {"llm_call": {"model": "job-model"}}
    await session.flush()

    resolved = await resolve_providers(session, organization, job)

    assert resolved.llm_call.model == "job-model"
    assert resolved.tts.voice_id == "org-voice"
    assert resolved.stt.model == "account-stt"


async def test_the_env_gateway_url_reaches_only_the_llm_slots(session, two_tenants, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    get_settings.cache_clear()
    organization, job = await fixtures(session, two_tenants)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.llm_call.base_url == "https://openrouter.ai/api/v1"
    assert resolved.llm_summary.base_url == "https://openrouter.ai/api/v1"
    assert resolved.stt.base_url is None
    assert resolved.tts.base_url is None
