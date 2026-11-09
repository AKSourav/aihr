import importlib

import pytest
from fastapi.testclient import TestClient

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


def test_dev_routes_are_absent_by_default(monkeypatch):
    client = TestClient(build_app(monkeypatch, DEV_ROUTES_ENABLED="false"))

    assert client.post("/dev/calls", json={"candidate_key": "x"}).status_code == 404
    assert "/dev/calls" not in client.get("/openapi.json").json()["paths"]


def test_dev_routes_require_the_internal_token(monkeypatch):
    client = TestClient(build_app(monkeypatch, DEV_ROUTES_ENABLED="true", INTERNAL_API_TOKEN="tok"))

    assert "/dev/calls" in client.get("/openapi.json").json()["paths"]
    assert client.post("/dev/calls", json={"candidate_key": "x"}).status_code == 401
    assert (
        client.post(
            "/dev/calls", json={"candidate_key": "x"}, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_internal_routes_require_the_internal_token(monkeypatch):
    client = TestClient(build_app(monkeypatch, INTERNAL_API_TOKEN="tok"))

    call_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/internal/calls/{call_id}/context").status_code == 401


def test_requests_over_the_limit_are_rejected(monkeypatch):
    client = TestClient(build_app(monkeypatch, RATE_LIMIT_PER_MINUTE="3"))

    codes = [client.get("/health").status_code for _ in range(5)]

    assert codes == [200, 200, 200, 429, 429]
