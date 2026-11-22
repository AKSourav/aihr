#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ROOT_SUB="33333333-3333-3333-3333-333333333333"
OWNER_SUB="66666666-6666-6666-6666-666666666666"

NOTES=""

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }
warn() {
  printf '\033[1;33mnote:\033[0m %s\n' "$*" >&2
  NOTES="$NOTES
    - $*"
}

command -v docker >/dev/null 2>&1 || die "docker is required but not installed"
docker info >/dev/null 2>&1 || die "docker daemon is not running"
[ -f .env ] || die "no .env file; run: cp .env.example .env"

env_keys() { grep -oE '^[A-Z_][A-Z0-9_]*' "$1" | sort -u; }
MISSING="$(comm -13 <(env_keys .env) <(env_keys .env.example) | tr '\n' ' ')"
[ -z "${MISSING// /}" ] || die "your .env predates .env.example and is missing: ${MISSING}
       the simplest fix is: cp .env.example .env"

command -v node >/dev/null 2>&1 || warn "node is not installed; 'make desktop' needs it"

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
MAILPIT_PORT="$(docker compose port mailpit 8025 | awk -F: '{print $NF}')"

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | awk '{print $1}'; }
[ "$(env_value PUBLIC_BASE_URL)" = "http://localhost:$API_PORT" ] || warn \
  "PUBLIC_BASE_URL is '$(env_value PUBLIC_BASE_URL)' but the API is published on $API_PORT; emailed invite links will not open"
[ -n "$(env_value LIVEKIT_API_KEY)" ] || warn \
  "LIVEKIT_API_KEY is unset; invites send and arrive, but a candidate clicking Join gets 503"

say "Waiting for Keycloak and the API"
wait_for "keycloak" "http://localhost:$KEYCLOAK_PORT/realms/aihr/.well-known/openid-configuration"
wait_for "api" "http://localhost:$API_PORT/health"

ISSUER="$(curl -s "http://localhost:$KEYCLOAK_PORT/realms/aihr/.well-known/openid-configuration" \
  | sed -n 's/.*"issuer":"\([^"]*\)".*/\1/p')"
[ "$ISSUER" = "http://localhost:$KEYCLOAK_PORT/realms/aihr" ] || warn \
  "Keycloak mints tokens with iss '$ISSUER'; KEYCLOAK_ISSUER_URL must match it or every token is rejected"

DESKTOP_CLIENT="$(curl -s -o /dev/null -w '%{http_code}' \
  "http://localhost:$KEYCLOAK_PORT/realms/aihr/protocol/openid-connect/auth?client_id=aihr-desktop&response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A47823%2Fcallback&scope=openid")"
[ "$DESKTOP_CLIENT" != "400" ] || warn \
  "the running realm has no aihr-desktop client, so 'make desktop' cannot sign in; recreate the volume:
      docker compose rm -sf keycloak keycloak-db && docker volume rm aihr_keycloakdata && make dev"

say "Running migrations and seed"
docker compose exec -T api alembic upgrade head
docker compose exec -T api python -m scripts.seed

say "Bootstrapping the account owner and the platform admin"
docker compose exec -T api python -m scripts.grant_membership \
  --sub "$OWNER_SUB" --email hr@acme.test --org "Acme Fintech" --role owner
docker compose exec -T api python -m scripts.grant_membership \
  --sub "$ROOT_SUB" --email root@aihr.test --platform-admin

ORG_ID="$(docker compose exec -T postgres \
  psql -U app -d hr -tAc "select id from organizations where name = 'Acme Fintech'" | tr -d '[:space:]')"
ACCOUNT_ID="$(docker compose exec -T postgres \
  psql -U app -d hr -tAc "select id from accounts where name = 'Acme Group'" | tr -d '[:space:]')"

say "Inviting the remaining users through the account API"
OWNER_TOKEN="$(docker compose exec -T api python -m scripts.get_token --username owner --password owner)"
invite() {
  curl -sf -o /dev/null -X POST "http://localhost:$API_PORT/accounts/$ACCOUNT_ID/invitations" \
    -H "Authorization: Bearer $OWNER_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"memberships\":[{\"org_id\":\"$ORG_ID\",\"role\":\"$2\"}]}" \
    && printf '  invited %s as %s\n' "$1" "$2" \
    || printf '  \033[1;33mfailed\033[0m to invite %s\n' "$1"
}
invite recruiter@acme.test recruiter
invite viewer@acme.test viewer

cat <<EOF

$(printf '\033[1;32mCompose stack is up.\033[0m')

  API           http://localhost:$API_PORT
  Keycloak      http://localhost:$KEYCLOAK_PORT/admin  (admin / admin)
  Mailpit       http://localhost:$MAILPIT_PORT
  Tenant        $ORG_ID

  Users (password == username, client aihr-local-cli):
    owner       account owner, invited the other two through the API
    recruiter   recruiter on Acme Fintech
    viewer      viewer on Acme Fintech
    root        platform admin, no membership
    outsider    no access at all

  Get a token and call the API:
    TOKEN=\$(make dev-token USER=recruiter)
    curl -s -H "Authorization: Bearer \$TOKEN" \\
      http://localhost:$API_PORT/tenants/$ORG_ID/applications | jq

  Recruiter desktop app:  make desktop   (sign in as recruiter / recruiter)

  The agent worker is behind a profile; start it once provider keys are set:
    docker compose --profile agent up -d agent-worker

  Verify the application layer:  make dev-verify
  Exercise the edge (gateway, OPA):  make k3d-up && make k3d-verify
${NOTES:+
  Before make desktop:$NOTES
}
EOF
