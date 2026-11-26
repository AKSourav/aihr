from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Account, Job, Organization, Secret
from core.schemas import ProviderSelection, ResolvedProvider, SlotConfig
from core.secrets import decrypt_secret
from core.settings import get_settings

SLOTS = ("stt", "tts", "llm_call", "llm_summary")

CATALOGUE: dict[str, dict[str, dict]] = {
    "stt": {
        "deepgram": {"label": "Deepgram", "field": "model", "options": ["nova-3", "nova-2", "enhanced"]},
        "openai": {"label": "Whisper (OpenAI)", "field": "model", "options": ["whisper-1"]},
    },
    "tts": {
        "elevenlabs": {
            "label": "ElevenLabs",
            "field": "voice_id",
            "options": ["Rachel", "Adam", "Antoni", "Bella"],
        },
        "cartesia": {
            "label": "Cartesia",
            "field": "voice_id",
            "options": ["British Lady", "Friendly Sidekick"],
        },
    },
    "llm_call": {
        "openai": {
            "label": "OpenAI-compatible",
            "field": "model",
            "options": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        },
    },
    "llm_summary": {
        "openai": {
            "label": "OpenAI-compatible",
            "field": "model",
            "options": ["gpt-4o", "gpt-4.1", "o3-mini"],
        },
    },
}

DEFAULT_PROVIDER = {"stt": "deepgram", "tts": "cartesia", "llm_call": "openai", "llm_summary": "openai"}

ENV_KEYS = {"deepgram": "deepgram_api_key", "cartesia": "tts_api_key", "openai": "llm_api_key"}

ENV_MODEL = {"llm_call": "llm_call_model", "llm_summary": "llm_summary_model"}


class ProviderNotConfigured(Exception):
    pass


@dataclass(frozen=True)
class ResolvedProviders:
    stt: ResolvedProvider
    tts: ResolvedProvider
    llm_call: ResolvedProvider
    llm_summary: ResolvedProvider


def catalogue() -> dict:
    return CATALOGUE


def merge_slot(job: SlotConfig, org: SlotConfig) -> SlotConfig:
    merged = {
        field: getattr(job, field) if getattr(job, field) is not None else getattr(org, field)
        for field in SlotConfig.model_fields
    }
    return SlotConfig(**merged)


def merge_selection(*configs: dict | None) -> ProviderSelection:
    merged = ProviderSelection()
    for config in reversed(configs):
        nearer = ProviderSelection.model_validate(config or {})
        merged = ProviderSelection(
            **{slot: merge_slot(getattr(nearer, slot), getattr(merged, slot)) for slot in SLOTS}
        )
    return merged


async def _secrets(session: AsyncSession, account_id: UUID) -> dict[UUID, Secret]:
    rows = (await session.scalars(select(Secret).where(Secret.account_id == account_id))).all()
    return {row.id: row for row in rows}


def _resolve_slot(slot: str, config: SlotConfig, secrets: dict[UUID, Secret]) -> ResolvedProvider:
    settings = get_settings()
    provider = config.provider or DEFAULT_PROVIDER[slot]
    if provider not in CATALOGUE[slot]:
        raise ProviderNotConfigured(f"{slot} provider {provider} is not supported")

    if config.secret_id is not None:
        secret = secrets.get(config.secret_id)
        if secret is None:
            raise ProviderNotConfigured(f"{slot} points at a secret that no longer exists")
        api_key = decrypt_secret(secret.ciphertext)
    else:
        env_field = ENV_KEYS.get(provider)
        api_key = getattr(settings, env_field) if env_field else ""
        if not api_key:
            raise ProviderNotConfigured(f"no secret configured for {slot}")

    model = config.model or (getattr(settings, ENV_MODEL[slot]) if slot in ENV_MODEL else "")
    voice_id = config.voice_id or (settings.tts_voice_id or None if slot == "tts" else None)

    return ResolvedProvider(
        provider=provider,
        model=model or "",
        api_key=api_key,
        base_url=config.base_url or (settings.llm_base_url or None if slot in ENV_MODEL else None),
        voice_id=voice_id,
    )


async def resolve_providers(
    session: AsyncSession, organization: Organization, job: Job
) -> ResolvedProviders:
    account = await session.get(Account, organization.account_id)
    if account is None:
        raise ProviderNotConfigured("organization has no account")

    selection = merge_selection(
        job.provider_config, organization.provider_config, account.provider_config
    )
    secrets = await _secrets(session, account.id)

    return ResolvedProviders(
        **{slot: _resolve_slot(slot, getattr(selection, slot), secrets) for slot in SLOTS}
    )
