import pytest

from core.schemas import ResolvedProvider
from services.agent import providers


@pytest.fixture
def recorded(monkeypatch):
    calls = {}

    def record(name):
        def build(**kwargs):
            calls[name] = kwargs
            return name

        return build

    monkeypatch.setattr(providers.deepgram, "STT", record("deepgram.STT"))
    monkeypatch.setattr(providers.openai, "STT", record("openai.STT"))
    monkeypatch.setattr(providers.openai, "LLM", record("openai.LLM"))
    monkeypatch.setattr(providers.cartesia, "TTS", record("cartesia.TTS"))
    monkeypatch.setattr(providers.elevenlabs, "TTS", record("elevenlabs.TTS"))
    return calls


def test_deepgram_stt_passes_the_model(recorded):
    providers.build_stt(ResolvedProvider(provider="deepgram", model="nova-3", api_key="k"))

    assert recorded["deepgram.STT"] == {"api_key": "k", "model": "nova-3"}


def test_deepgram_stt_omits_an_unset_model(recorded):
    providers.build_stt(ResolvedProvider(provider="deepgram", api_key="k"))

    assert recorded["deepgram.STT"] == {"api_key": "k"}


def test_whisper_stt_defaults_its_model(recorded):
    providers.build_stt(ResolvedProvider(provider="openai", api_key="k"))

    assert recorded["openai.STT"]["model"] == "whisper-1"


def test_cartesia_uses_the_voice_kwarg(recorded):
    providers.build_tts(ResolvedProvider(provider="cartesia", api_key="k", voice_id="v"))

    assert recorded["cartesia.TTS"]["voice"] == "v"
    assert "voice_id" not in recorded["cartesia.TTS"]


def test_elevenlabs_uses_the_voice_id_kwarg(recorded):
    providers.build_tts(ResolvedProvider(provider="elevenlabs", api_key="k", voice_id="v"))

    assert recorded["elevenlabs.TTS"]["voice_id"] == "v"
    assert "voice" not in recorded["elevenlabs.TTS"]


def test_llm_passes_base_url_when_set(recorded):
    providers.build_llm(
        ResolvedProvider(provider="openai", model="m", api_key="k", base_url="https://gw.test/v1")
    )

    assert recorded["openai.LLM"]["base_url"] == "https://gw.test/v1"
    assert recorded["openai.LLM"]["max_completion_tokens"] == providers.MAX_COMPLETION_TOKENS


def test_llm_omits_base_url_when_unset(recorded):
    providers.build_llm(ResolvedProvider(provider="openai", model="m", api_key="k"))

    assert "base_url" not in recorded["openai.LLM"]


@pytest.mark.parametrize(
    "build,provider",
    [
        (providers.build_stt, "elevenlabs"),
        (providers.build_tts, "deepgram"),
        (providers.build_llm, "cartesia"),
    ],
)
def test_an_unknown_provider_raises(recorded, build, provider):
    with pytest.raises(providers.UnknownProvider):
        build(ResolvedProvider(provider=provider, api_key="k"))
