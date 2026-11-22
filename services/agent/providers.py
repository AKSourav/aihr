from typing import Any

from livekit.agents import llm as agent_llm
from livekit.agents import stt as agent_stt
from livekit.agents import tts as agent_tts
from livekit.plugins import cartesia, deepgram, elevenlabs, openai

from core.schemas import ResolvedProvider

MAX_COMPLETION_TOKENS = 300


class UnknownProvider(Exception):
    pass


def build_stt(config: ResolvedProvider) -> agent_stt.STT:
    kwargs: dict[str, Any] = {"api_key": config.api_key}
    if config.model:
        kwargs["model"] = config.model
    if config.base_url:
        kwargs["base_url"] = config.base_url

    if config.provider == "deepgram":
        return deepgram.STT(**kwargs)
    if config.provider == "openai":
        kwargs.setdefault("model", "whisper-1")
        return openai.STT(**kwargs)
    raise UnknownProvider(f"unsupported stt provider {config.provider}")


def build_tts(config: ResolvedProvider) -> agent_tts.TTS:
    kwargs: dict[str, Any] = {"api_key": config.api_key}
    if config.model:
        kwargs["model"] = config.model

    if config.provider == "cartesia":
        if config.voice_id:
            kwargs["voice"] = config.voice_id
        return cartesia.TTS(**kwargs)

    if config.provider == "elevenlabs":
        if config.voice_id:
            kwargs["voice_id"] = config.voice_id
        return elevenlabs.TTS(**kwargs)

    raise UnknownProvider(f"unsupported tts provider {config.provider}")


def build_llm(config: ResolvedProvider) -> agent_llm.LLM:
    if config.provider != "openai":
        raise UnknownProvider(f"unsupported llm provider {config.provider}")

    kwargs: dict[str, Any] = {
        "model": config.model,
        "api_key": config.api_key,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return openai.LLM(**kwargs)
