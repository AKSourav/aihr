from cryptography.fernet import Fernet, InvalidToken

from core.settings import get_settings

KEY_VERSION = 1


class SecretDecryptionError(Exception):
    pass


def _fernet() -> Fernet:
    key = get_settings().provider_encryption_key
    if not key:
        raise RuntimeError("PROVIDER_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("PROVIDER_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError("stored credential could not be decrypted") from exc


def last4(plaintext: str) -> str:
    return plaintext[-4:]
