from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Job, Organization, ProviderCredential
from core.schemas import ProviderSelection, ResolvedProvider
from core.secrets import decrypt_secret
from core.settings import get_settings

STT_PROVIDERS = ("deepgram", "openai")
TTS_PROVIDERS = ("cartesia", "elevenlabs")
LLM_PROVIDERS = ("openai",)

DEFAULT_STT = "deepgram"
DEFAULT_TTS = "cartesia"
DEFAULT_LLM = "openai"

ENV_KEYS = {
    "deepgram": "deepgram_api_key",
    "cartesia": "tts_api_key",
    "openai": "llm_api_key",
}


class ProviderNotConfigured(Exception):
    pass


@dataclass(frozen=True)
class ResolvedProviders:
    stt: ResolvedProvider
    tts: ResolvedProvider
    llm_call: ResolvedProvider
    llm_summary: ResolvedProvider


def merge_selection(job_config: dict | None, org_config: dict | None) -> ProviderSelection:
    job = ProviderSelection.model_validate(job_config or {})
    org = ProviderSelection.model_validate(org_config or {})
    merged = {
        field: getattr(job, field) if getattr(job, field) is not None else getattr(org, field)
        for field in ProviderSelection.model_fields
    }
    return ProviderSelection(**merged)


async def _credentials(
    session: AsyncSession, org_id, job_id
) -> dict[tuple[str, bool], ProviderCredential]:
    rows = (
        await session.scalars(
            select(ProviderCredential).where(
                ProviderCredential.org_id == org_id,
                or_(
                    ProviderCredential.job_id.is_(None),
                    ProviderCredential.job_id == job_id,
                ),
            )
        )
    ).all()
    return {(row.provider, row.job_id is not None): row for row in rows}


def _resolve_one(
    provider: str,
    model: str | None,
    voice_id: str | None,
    base_url: str | None,
    credentials: dict[tuple[str, bool], ProviderCredential],
) -> ResolvedProvider:
    settings = get_settings()
    credential = credentials.get((provider, True)) or credentials.get((provider, False))

    if credential is not None:
        api_key = decrypt_secret(credential.secret_ciphertext)
        base_url = credential.base_url or base_url
    else:
        env_field = ENV_KEYS.get(provider)
        api_key = getattr(settings, env_field) if env_field else ""
        if not api_key:
            raise ProviderNotConfigured(f"no credential configured for {provider}")
        base_url = base_url or (settings.llm_base_url or None)

    return ResolvedProvider(
        provider=provider,
        model=model or "",
        api_key=api_key,
        base_url=base_url,
        voice_id=voice_id,
    )


async def resolve_providers(
    session: AsyncSession, organization: Organization, job: Job
) -> ResolvedProviders:
    selection = merge_selection(job.provider_config, organization.provider_config)
    settings = get_settings()
    credentials = await _credentials(session, organization.id, job.id)

    stt_provider = selection.stt_provider or DEFAULT_STT
    tts_provider = selection.tts_provider or DEFAULT_TTS
    llm_provider = selection.llm_provider or DEFAULT_LLM

    for provider, supported in (
        (stt_provider, STT_PROVIDERS),
        (tts_provider, TTS_PROVIDERS),
        (llm_provider, LLM_PROVIDERS),
    ):
        if provider not in supported:
            raise ProviderNotConfigured(f"unsupported provider {provider}")

    llm_base_url = selection.llm_base_url

    return ResolvedProviders(
        stt=_resolve_one(stt_provider, selection.stt_model, None, None, credentials),
        tts=_resolve_one(
            tts_provider,
            selection.tts_model,
            selection.tts_voice_id or settings.tts_voice_id or None,
            None,
            credentials,
        ),
        llm_call=_resolve_one(
            llm_provider,
            selection.llm_call_model or settings.llm_call_model,
            None,
            llm_base_url,
            credentials,
        ),
        llm_summary=_resolve_one(
            llm_provider,
            selection.llm_summary_model or settings.llm_summary_model,
            None,
            llm_base_url,
            credentials,
        ),
    )
