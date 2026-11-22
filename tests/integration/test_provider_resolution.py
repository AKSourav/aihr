import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Job, Organization, ProviderCredential
from core.providers import ProviderNotConfigured, resolve_providers
from core.secrets import encrypt_secret, last4
from core.settings import get_settings

ORG_KEY = "org-key-aaaa"
JOB_KEY = "job-key-bbbb"
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


async def credential(session: AsyncSession, org_id, provider, secret, job_id=None):
    row = ProviderCredential(
        id=uuid.uuid4(),
        org_id=org_id,
        job_id=job_id,
        provider=provider,
        secret_ciphertext=encrypt_secret(secret),
        secret_last4=last4(secret),
    )
    session.add(row)
    await session.flush()
    return row


async def test_the_platform_env_key_is_used_when_nothing_is_configured(session, two_tenants):
    organization, job = await fixtures(session, two_tenants)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.stt.provider == "deepgram"
    assert resolved.stt.api_key == ENV_KEY
    assert resolved.llm_summary.model == "env-summary-model"


async def test_an_org_credential_beats_the_env_key(session, two_tenants):
    organization, job = await fixtures(session, two_tenants)
    await credential(session, organization.id, "deepgram", ORG_KEY)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.stt.api_key == ORG_KEY


async def test_a_job_credential_beats_the_org_credential(session, two_tenants):
    organization, job = await fixtures(session, two_tenants)
    await credential(session, organization.id, "deepgram", ORG_KEY)
    await credential(session, organization.id, "deepgram", JOB_KEY, job_id=job.id)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.stt.api_key == JOB_KEY


async def test_a_job_selection_overrides_the_org_selection_field_by_field(session, two_tenants):
    organization, job = await fixtures(
        session,
        two_tenants,
        job_config={"llm_call_model": "job-model"},
        org_config={"llm_call_model": "org-model", "tts_voice_id": "org-voice"},
    )

    resolved = await resolve_providers(session, organization, job)

    assert resolved.llm_call.model == "job-model"
    assert resolved.tts.voice_id == "org-voice"


async def test_elevenlabs_without_a_credential_fails_closed(session, two_tenants):
    organization, job = await fixtures(session, two_tenants, job_config={"tts_provider": "elevenlabs"})

    with pytest.raises(ProviderNotConfigured):
        await resolve_providers(session, organization, job)


async def test_elevenlabs_resolves_when_a_credential_exists(session, two_tenants):
    organization, job = await fixtures(session, two_tenants, job_config={"tts_provider": "elevenlabs"})
    await credential(session, organization.id, "elevenlabs", ORG_KEY)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.tts.provider == "elevenlabs"
    assert resolved.tts.api_key == ORG_KEY


async def test_an_undecryptable_credential_fails_closed(session, two_tenants, monkeypatch):
    organization, job = await fixtures(session, two_tenants)
    await credential(session, organization.id, "deepgram", ORG_KEY)

    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    from core.secrets import SecretDecryptionError

    with pytest.raises(SecretDecryptionError):
        await resolve_providers(session, organization, job)


async def test_an_unsupported_provider_is_rejected(session, two_tenants):
    organization, job = await fixtures(session, two_tenants, job_config={"tts_provider": "nope"})

    with pytest.raises(ProviderNotConfigured):
        await resolve_providers(session, organization, job)


async def test_another_orgs_credential_is_not_used(session, two_tenants):
    organization, job = await fixtures(session, two_tenants)
    await credential(session, two_tenants["other_org"], "deepgram", ORG_KEY)

    resolved = await resolve_providers(session, organization, job)

    assert resolved.stt.api_key == ENV_KEY
