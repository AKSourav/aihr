import logging
import time

import httpx

from core.settings import get_settings

logger = logging.getLogger(__name__)

TIMEOUT = 20.0
EXPIRY_MARGIN_SECONDS = 30

_token: str = ""
_expires_at: float = 0.0


class KeycloakError(RuntimeError):
    pass


def realm_url() -> str:
    settings = get_settings()
    return f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"


def admin_url() -> str:
    settings = get_settings()
    return f"{settings.keycloak_url}/admin/realms/{settings.keycloak_realm}"


def reset_token_cache() -> None:
    global _token, _expires_at
    _token = ""
    _expires_at = 0.0


async def access_token(client: httpx.AsyncClient) -> str:
    global _token, _expires_at
    if _token and time.monotonic() < _expires_at:
        return _token

    settings = get_settings()
    if not settings.keycloak_admin_client_secret:
        raise KeycloakError("KEYCLOAK_ADMIN_CLIENT_SECRET is not configured")

    response = await client.post(
        f"{realm_url()}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": settings.keycloak_admin_client_id,
            "client_secret": settings.keycloak_admin_client_secret,
        },
    )
    if response.status_code != 200:
        raise KeycloakError(f"admin token request failed: {response.status_code}")

    payload = response.json()
    _token = payload["access_token"]
    _expires_at = time.monotonic() + max(payload.get("expires_in", 60) - EXPIRY_MARGIN_SECONDS, 0)
    return _token


async def find_user_by_email(client: httpx.AsyncClient, email: str) -> dict | None:
    response = await client.get(
        f"{admin_url()}/users",
        params={"email": email, "exact": "true"},
        headers={"Authorization": f"Bearer {await access_token(client)}"},
    )
    if response.status_code != 200:
        raise KeycloakError(f"user lookup failed: {response.status_code}")
    users = response.json()
    return users[0] if users else None


async def create_user(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post(
        f"{admin_url()}/users",
        headers={"Authorization": f"Bearer {await access_token(client)}"},
        json={"username": email, "email": email, "enabled": True, "emailVerified": False},
    )
    if response.status_code != 201:
        raise KeycloakError(f"user creation failed: {response.status_code}")

    location = response.headers.get("location", "")
    subject = location.rsplit("/", 1)[-1]
    if not subject:
        raise KeycloakError("user creation returned no location header")
    return subject


async def send_invitation_email(client: httpx.AsyncClient, subject: str) -> bool:
    response = await client.put(
        f"{admin_url()}/users/{subject}/execute-actions-email",
        headers={"Authorization": f"Bearer {await access_token(client)}"},
        json=["UPDATE_PASSWORD", "VERIFY_EMAIL"],
    )
    if response.status_code not in (200, 204):
        logger.warning(
            "invitation email was not sent",
            extra={"keycloak_sub": subject, "status": response.status_code},
        )
        return False
    return True
