from core.providers import merge_selection


def test_a_job_field_beats_the_org_field():
    merged = merge_selection({"tts_voice_id": "job-voice"}, {"tts_voice_id": "org-voice"})

    assert merged.tts_voice_id == "job-voice"


def test_unset_job_fields_fall_back_to_the_org_field_by_field():
    merged = merge_selection(
        {"llm_call_model": "job-model"},
        {"tts_voice_id": "org-voice", "stt_provider": "deepgram", "llm_call_model": "org-model"},
    )

    assert merged.llm_call_model == "job-model"
    assert merged.tts_voice_id == "org-voice"
    assert merged.stt_provider == "deepgram"


def test_an_empty_job_config_inherits_the_whole_org_config():
    merged = merge_selection(None, {"tts_provider": "elevenlabs", "tts_voice_id": "rachel"})

    assert merged.tts_provider == "elevenlabs"
    assert merged.tts_voice_id == "rachel"


def test_everything_is_none_when_neither_is_set():
    merged = merge_selection(None, None)

    assert merged.tts_provider is None
    assert merged.llm_summary_model is None


def test_unknown_keys_in_stored_config_are_ignored():
    merged = merge_selection({"tts_provider": "cartesia", "legacy_field": "x"}, None)

    assert merged.tts_provider == "cartesia"
