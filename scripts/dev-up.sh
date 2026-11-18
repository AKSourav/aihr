#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RECRUITER_SUB="11111111-1111-1111-1111-111111111111"
VIEWER_SUB="22222222-2222-2222-2222-222222222222"
ROOT_SUB="33333333-3333-3333-3333-333333333333"
OUTSIDER_SUB="44444444-4444-4444-4444-444444444444"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is required but not installed"
docker info >/dev/null 2>&1 || die "docker daemon is not running"
[ -f .env ] || die "no .env file; run: cp .env.example .env"

wait_for() {
  local name="$1" url="$2" tries="${3:-120}"
  printf '  waiting for %s' "$name"
  for _ in $(seq "$tries"); do
    if curl -sf -o /dev/null "$url"; then
      printf ' ok\n'
      return 0
    fi
    printf '.'
    sleep 2
  done
  printf '\n'
  die "$name did not become ready at $url"
}

say "Starting services"
docker compose up -d --build

API_PORT="$(docker compose port api 8000 | awk -F: '{print $NF}')"
KEYCLOAK_PORT="$(docker compose port keycloak 8080 | awk -F: '{print $NF}')"

say "Waiting for Keycloak and the API"
wait_for "keycloak" "http://localhost:$KEYCLOAK_PORT/realms/aihr/.well-known/openid-configuration"
wait_for "api" "http://localhost:$API_PORT/health"

say "Running migrations and seed"
docker compose exec -T api alembic upgrade head
docker compose exec -T api python -m scripts.seed

say "Granting tenant access to the realm users"
docker compose exec -T api python -m scripts.grant_membership \
  --sub "$RECRUITER_SUB" --email recruiter@acme.test --org "Acme Fintech" --role recruiter
docker compose exec -T api python -m scripts.grant_membership \
  --sub "$VIEWER_SUB" --email viewer@acme.test --org "Acme Fintech" --role viewer
docker compose exec -T api python -m scripts.grant_membership \
  --sub "$ROOT_SUB" --email root@aihr.test --platform-admin
docker compose exec -T api python -m scripts.grant_membership \
  --sub "$OUTSIDER_SUB" --email outsider@nowhere.test

ORG_ID="$(docker compose exec -T postgres \
  psql -U app -d hr -tAc "select id from organizations where name = 'Acme Fintech'" | tr -d '[:space:]')"

cat <<EOF

$(printf '\033[1;32mCompose stack is up.\033[0m')

  API           http://localhost:$API_PORT
  Keycloak      http://keycloak:$KEYCLOAK_PORT/admin  (admin / admin)
                needs: echo '127.0.0.1 keycloak' | sudo tee -a /etc/hosts
  Tenant        $ORG_ID

  Users (password == username, client aihr-local-cli):
    recruiter   recruiter on Acme Fintech
    viewer      viewer on Acme Fintech
    root        platform admin, no membership
    outsider    no access at all

  Tokens must be requested from inside the Compose network — the issuer is
  pinned to http://keycloak:8080, so a token fetched from the host against
  localhost:8080 is rejected:

    TOKEN=\$(make dev-token USER=recruiter)
    curl -s -H "Authorization: Bearer \$TOKEN" \\
      http://localhost:$API_PORT/tenants/$ORG_ID/applications | jq

  The agent worker is behind a profile; start it once provider keys are set:
    docker compose --profile agent up -d agent-worker

  Verify the application layer:  make dev-verify
  Exercise the edge (gateway, OPA):  make k3d-up && make k3d-verify
EOF
