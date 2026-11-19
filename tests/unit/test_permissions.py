import importlib
import re

import pytest
from fastapi.testclient import TestClient

from core.permissions import (
    ACCOUNT_ROLE_PERMISSIONS,
    ACCOUNT_ROUTES,
    PLATFORM_ROUTES,
    ROLE_PERMISSIONS,
    ROUTES,
    SENSITIVE,
    Permission,
    account_grants,
    grants,
    path_pattern,
    role_permission_table,
    route_table,
)
from core.settings import get_settings

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


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


def test_path_pattern_captures_the_tenant():
    pattern = path_pattern("/tenants/{org_id}/calls")
    match = re.match(pattern, "/tenants/abc/calls")

    assert pattern == "^/tenants/([^/]+)/calls$"
    assert match is not None
    assert match.group(1) == "abc"


def test_path_pattern_does_not_capture_other_parameters():
    pattern = path_pattern("/tenants/{org_id}/calls/{call_id}")
    match = re.match(pattern, "/tenants/abc/calls/xyz")

    assert match is not None
    assert match.group(1) == "abc"
    assert match.groups() == ("abc",)


def test_path_pattern_is_anchored():
    pattern = path_pattern("/tenants/{org_id}/calls")

    assert re.match(pattern, "/tenants/abc/calls/extra") is None
    assert re.match(pattern, "/prefix/tenants/abc/calls") is None


def openapi_paths(monkeypatch, **env):
    app = build_app(monkeypatch, **env)
    with TestClient(app) as client:
        return client.get("/openapi.json").json()["paths"]


def test_every_mapped_route_is_registered_in_the_app(monkeypatch):
    paths = openapi_paths(monkeypatch, DEV_ROUTES_ENABLED="true", INTERNAL_API_TOKEN="tok")

    for method, template, _ in ROUTES:
        assert template in paths, f"{template} is mapped to a permission but not served"
        assert method.lower() in paths[template]


def test_every_tenant_route_has_a_permission(monkeypatch):
    paths = openapi_paths(monkeypatch, DEV_ROUTES_ENABLED="true", INTERNAL_API_TOKEN="tok")
    mapped = {(method, template) for method, template, _ in ROUTES}

    for template, operations in paths.items():
        if not template.startswith("/tenants/"):
            continue
        for method in operations:
            if method.upper() not in HTTP_METHODS:
                continue
            assert (method.upper(), template) in mapped, f"{method} {template} has no permission"


def test_every_account_route_is_registered_in_the_app(monkeypatch):
    paths = openapi_paths(monkeypatch, DEV_ROUTES_ENABLED="true", INTERNAL_API_TOKEN="tok")

    for method, template, _ in ACCOUNT_ROUTES:
        assert template in paths, f"{template} is mapped to a permission but not served"
        assert method.lower() in paths[template]


def test_every_account_route_has_a_permission(monkeypatch):
    paths = openapi_paths(monkeypatch, DEV_ROUTES_ENABLED="true", INTERNAL_API_TOKEN="tok")
    mapped = {(method, template) for method, template, _ in ACCOUNT_ROUTES}

    for template, operations in paths.items():
        if not template.startswith("/accounts/"):
            continue
        for method in operations:
            if method.upper() not in HTTP_METHODS:
                continue
            assert (method.upper(), template) in mapped, f"{method} {template} has no permission"


def test_account_routes_capture_the_account(monkeypatch):
    for _, template, _ in ACCOUNT_ROUTES:
        pattern = path_pattern(template, capture="account_id")
        probe = template.replace("{account_id}", "acc-1").replace("{user_id}", "u-1")
        match = re.match(pattern, probe)

        assert match is not None
        assert match.groups() == ("acc-1",)


def test_route_table_scopes_every_entry():
    scopes = {entry["pattern"]: entry["scope"] for entry in route_table()}

    assert scopes["^/tenants/([^/]+)/calls$"] == "tenant"
    assert scopes["^/accounts/([^/]+)/members$"] == "account"


def test_every_platform_route_is_registered_in_the_app(monkeypatch):
    paths = openapi_paths(monkeypatch, DEV_ROUTES_ENABLED="true", INTERNAL_API_TOKEN="tok")

    for method, template in PLATFORM_ROUTES:
        assert template in paths, f"{template} is a platform route but not served"
        assert method.lower() in paths[template]


def test_platform_routes_are_matched_by_their_pattern():
    for method, template in PLATFORM_ROUTES:
        assert re.match(path_pattern(template), template)


def test_role_permission_table_covers_every_role():
    table = role_permission_table()

    for role in ROLE_PERMISSIONS:
        assert role in table


def test_request_roles_match_the_permission_tables():
    from typing import get_args

    from services.api.routes.account import AccountRole, TenantRole

    assert set(get_args(TenantRole)) == set(ROLE_PERMISSIONS)
    assert set(get_args(AccountRole)) == set(ACCOUNT_ROLE_PERMISSIONS)


def test_role_permission_table_has_no_account_keys():
    table = role_permission_table()

    assert not [key for key in table if key.startswith("account:")]


def test_route_table_entries_are_serialisable_strings():
    for entry in route_table():
        assert isinstance(entry["permission"], str)
        assert entry["pattern"].startswith("^")
        assert entry["method"] in HTTP_METHODS


def test_viewer_cannot_read_transcripts_or_create_calls():
    assert grants("viewer", Permission.SCORE_READ)
    assert not grants("viewer", Permission.TRANSCRIPT_READ)
    assert not grants("viewer", Permission.CALL_CREATE)


def test_account_owner_has_no_tenant_data_permissions():
    assert account_grants("owner", Permission.TENANT_CREATE)
    assert not account_grants("owner", Permission.TRANSCRIPT_READ)
    assert not account_grants("owner", Permission.SCORE_READ)


def test_unknown_roles_grant_nothing():
    assert not grants("superuser", Permission.SCORE_READ)
    assert not account_grants("superuser", Permission.TENANT_CREATE)


def test_sensitive_permissions_are_a_subset_of_all_permissions():
    assert SENSITIVE
    assert SENSITIVE <= set(Permission)
