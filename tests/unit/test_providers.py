import uuid

from core.providers import CATALOGUE, SLOTS, merge_selection


def test_a_job_field_beats_the_org_field():
    merged = merge_selection({"tts": {"voice_id": "Adam"}}, {"tts": {"voice_id": "Rachel"}})

    assert merged.tts.voice_id == "Adam"


def test_unset_job_fields_fall_back_field_by_field_within_a_slot():
    secret = str(uuid.uuid4())
    merged = merge_selection(
        {"tts": {"voice_id": "Adam"}},
        {"tts": {"provider": "elevenlabs", "voice_id": "Rachel", "secret_id": secret}},
    )

    assert merged.tts.voice_id == "Adam"
    assert merged.tts.provider == "elevenlabs"
    assert str(merged.tts.secret_id) == secret


def test_overriding_one_slot_leaves_the_others_inherited():
    merged = merge_selection(
        {"llm_call": {"model": "gpt-4o"}},
        {"tts": {"voice_id": "Rachel"}, "stt": {"model": "nova-3"}},
    )

    assert merged.llm_call.model == "gpt-4o"
    assert merged.tts.voice_id == "Rachel"
    assert merged.stt.model == "nova-3"


def test_an_empty_job_config_inherits_the_whole_org_config():
    merged = merge_selection(None, {"tts": {"provider": "elevenlabs", "voice_id": "Bella"}})

    assert merged.tts.provider == "elevenlabs"
    assert merged.tts.voice_id == "Bella"


def test_everything_is_none_when_neither_is_set():
    merged = merge_selection(None, None)

    assert merged.tts.provider is None
    assert merged.llm_summary.model is None


def test_unknown_keys_in_stored_config_are_ignored():
    merged = merge_selection({"tts": {"provider": "cartesia", "legacy": "x"}}, None)

    assert merged.tts.provider == "cartesia"


def test_the_catalogue_covers_every_slot():
    assert set(CATALOGUE) == set(SLOTS)
    for slot, providers in CATALOGUE.items():
        assert providers, slot
        for name, spec in providers.items():
            assert spec["field"] in ("model", "voice_id"), (slot, name)
            assert spec["options"], (slot, name)
            assert spec["label"], (slot, name)
