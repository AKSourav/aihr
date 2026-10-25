from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    temporal_address: str
    temporal_namespace: str = "default"

    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str

    api_internal_url: str
    internal_api_token: str

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_sip_trunk_id: str = ""
    agent_name: str = "hr-screener"

    deepgram_api_key: str = ""
    llm_api_key: str = ""
    llm_call_model: str = ""
    llm_summary_model: str = ""
    tts_api_key: str = ""
    tts_voice_id: str = ""

    call_mode: str = "web"
    call_signal_timeout_min: int = 45
    max_call_duration_min: int = 20
    recording_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
