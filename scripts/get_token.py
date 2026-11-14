import argparse

import httpx

from core.settings import get_settings


def fetch_token(username: str, password: str, client_id: str) -> str:
    settings = get_settings()
    url = f"{settings.keycloak_url}/realms/{settings.keycloak_realm}/protocol/openid-connect/token"
    response = httpx.post(
        url,
        data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
        },
        timeout=20.0,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a Keycloak access token (local dev only).")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--client-id", default="aihr-local-cli")
    args = parser.parse_args()

    print(fetch_token(args.username, args.password, args.client_id))


if __name__ == "__main__":
    main()
