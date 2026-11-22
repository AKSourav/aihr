import logging

import pytest
from cryptography.fernet import Fernet

from core.logging import JsonFormatter
from core.schemas import ResolvedProvider
from core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret, last4
from core.settings import get_settings

SECRET = "sk-live-abcd1234efgh"


@pytest.fixture
def key(monkeypatch):
    value = Fernet.generate_key().decode()
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", value)
    get_settings.cache_clear()
    yield value
    get_settings.cache_clear()


def test_round_trip(key):
    assert decrypt_secret(encrypt_secret(SECRET)) == SECRET


def test_ciphertext_does_not_contain_the_plaintext(key):
    assert SECRET not in encrypt_secret(SECRET)


def test_the_same_plaintext_encrypts_differently_each_time(key):
    assert encrypt_secret(SECRET) != encrypt_secret(SECRET)


def test_a_different_key_cannot_decrypt(key, monkeypatch):
    ciphertext = encrypt_secret(SECRET)
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()

    with pytest.raises(SecretDecryptionError):
        decrypt_secret(ciphertext)


def test_a_missing_key_raises_at_call_time(monkeypatch):
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError):
        encrypt_secret(SECRET)


def test_a_malformed_key_raises(monkeypatch):
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", "not-a-fernet-key")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError):
        encrypt_secret(SECRET)


def test_last4():
    assert last4(SECRET) == "efgh"


def test_the_api_key_is_absent_from_repr():
    provider = ResolvedProvider(provider="deepgram", model="nova-3", api_key=SECRET)

    assert SECRET not in repr(provider)
    assert "deepgram" in repr(provider)


def test_the_api_key_is_absent_from_a_formatted_log_record():
    provider = ResolvedProvider(provider="deepgram", api_key=SECRET)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "resolved", None, None)
    record.providers = provider

    assert SECRET not in JsonFormatter().format(record)
