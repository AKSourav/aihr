import importlib

import pytest
from fastapi.testclient import TestClient

from core.permissions import SELF_ROUTES, path_pattern, self_route_table
from core.settings import get_settings


def build_app(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    import services.api.main as api_main

    return importlib.reload(api_main).app


@pytest.fixture(autouse=True)
def clear_settings_cache():
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch):
    return TestClient(build_app(monkeypatch, INTERNAL_API_TOKEN="tok", INVITE_SIGNING_KEY="k" * 32))


def test_join_page_is_served_without_auth(client):
    response = client.get("/join/anything")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "livekit-client" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_an_invalid_invite_token_is_not_found(client):
    assert client.get("/public/invites/not-a-token").status_code == 404
    assert client.post("/public/invites/not-a-token/join").status_code == 404


def test_starting_a_screening_requires_a_bearer_token(client):
    org = "00000000-0000-0000-0000-0000000000aa"
    app_id = "00000000-0000-0000-0000-0000000000bb"

    response = client.post(f"/tenants/{org}/applications/{app_id}/screenings", json={"channel": "email"})

    assert response.status_code == 401


def test_me_requires_a_bearer_token(client):
    assert client.get("/me").status_code == 401


def test_every_self_route_is_served(client):
    paths = client.get("/openapi.json").json()["paths"]

    for method, template in SELF_ROUTES:
        assert template in paths
        assert method.lower() in paths[template]


def test_self_route_table_is_anchored():
    assert self_route_table() == [
        {"method": "GET", "pattern": path_pattern("/me")},
        {"method": "POST", "pattern": path_pattern("/organizations")},
        {"method": "GET", "pattern": path_pattern("/provider-catalogue")},
    ]
    assert path_pattern("/me") == "^/me$"
    assert path_pattern("/organizations") == "^/organizations$"
