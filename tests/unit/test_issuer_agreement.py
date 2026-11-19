import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = "http://localhost:8080"


def compose_kc_hostname() -> str:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    value = compose["services"]["keycloak"]["environment"]["KC_HOSTNAME"]
    return re.sub(r"\$\{KEYCLOAK_PORT:-(\d+)\}", r"\1", value)


def k3d_overlay() -> list[dict]:
    text = (ROOT / "deploy" / "local" / "kustomization.yaml").read_text()
    return [patch for patch in yaml.safe_load(text)["patches"]]


def securitypolicy_issuer() -> str:
    for patch in k3d_overlay():
        if patch["target"]["kind"] != "SecurityPolicy":
            continue
        for op in yaml.safe_load(patch["patch"]):
            if op["path"].endswith("/issuer"):
                return op["value"]
    raise AssertionError("the local overlay no longer patches the SecurityPolicy issuer")


def k3d_kc_hostname() -> str:
    deployment = next(
        doc
        for doc in yaml.safe_load_all((ROOT / "deploy" / "local" / "keycloak.yaml").read_text())
        if doc and doc["kind"] == "Deployment"
    )
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return next(entry["value"] for entry in env if entry["name"] == "KC_HOSTNAME")


def env_example_raw() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def env_example() -> dict[str, str]:
    return {key: raw.split("#")[0].strip() for key, raw in env_example_raw().items()}


def test_compose_and_the_env_example_agree_on_the_issuer():
    assert compose_kc_hostname() == EXPECTED
    assert env_example()["KEYCLOAK_ISSUER_URL"] == EXPECTED


def test_the_gateway_validates_the_issuer_keycloak_mints():
    assert k3d_kc_hostname() == EXPECTED
    assert securitypolicy_issuer() == f"{EXPECTED}/realms/aihr"


def test_keys_are_still_fetched_over_the_internal_network():
    assert env_example()["KEYCLOAK_URL"] == "http://keycloak:8080"

    for patch in k3d_overlay():
        if patch["target"]["kind"] != "SecurityPolicy":
            continue
        uris = [
            op["value"]
            for op in yaml.safe_load(patch["patch"])
            if op["path"].endswith("/uri")
        ]
        assert uris and all("keycloak" in uri and "localhost" not in uri for uri in uris)


def test_no_env_value_is_a_stray_comment():
    for key, raw in env_example_raw().items():
        assert not raw.strip().startswith("#"), (
            f"{key}= has nothing before its inline comment; compose would set it to the comment"
        )
