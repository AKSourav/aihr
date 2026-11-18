#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="$(docker compose port api 8000 2>/dev/null | awk -F: '{print $NF}')"
[ -n "$API_PORT" ] || { echo "the api container is not running; run: make dev"; exit 1; }
BASE="http://localhost:$API_PORT"
ROOT_SUB="33333333-3333-3333-3333-333333333333"

pass=0
fail=0

token_for() {
  docker compose exec -T api python -m scripts.get_token --username "$1" --password "$1" 2>/dev/null
}

psql_tac() {
  docker compose exec -T postgres psql -U app -d hr -tAc "$1" | tr -d '[:space:]'
}

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    printf '  \033[1;32mPASS\033[0m  %-52s %s\n' "$name" "$actual"
    pass=$((pass + 1))
  else
    printf '  \033[1;31mFAIL\033[0m  %-52s got %s, want %s\n' "$name" "$actual" "$expected"
    fail=$((fail + 1))
  fi
}

status() {
  curl -s -o /dev/null -w '%{http_code}' "$@"
}

ORG="$(psql_tac "select id from organizations where name = 'Acme Fintech'")"
OTHER="00000000-0000-0000-0000-0000000000ff"
[ -n "$ORG" ] || { echo "no seeded tenant; is the stack up? run: make dev"; exit 1; }

BG_OPEN="{\"org_id\":\"$ORG\",\"reason\":\"verification run from dev-verify\"}"
BG_DENIED="{\"org_id\":\"$ORG\",\"reason\":\"should not be permitted\"}"

RECRUITER="$(token_for recruiter)"
VIEWER="$(token_for viewer)"
ROOT_TOKEN="$(token_for root)"
OUTSIDER="$(token_for outsider)"

[ -n "$RECRUITER" ] || { echo "could not obtain a token; is the stack up? run: make dev"; exit 1; }

docker compose exec -T postgres psql -U app -d hr -q -c \
  "delete from break_glass_scopes where user_id in (select id from users where keycloak_sub = '$ROOT_SUB')" \
  >/dev/null

printf '\n\033[1mAuthentication\033[0m\n'
check "no token is rejected" 401 \
  "$(status "$BASE/tenants/$ORG/applications")"
check "a garbage token is rejected" 401 \
  "$(status -H 'Authorization: Bearer not.a.jwt' "$BASE/tenants/$ORG/applications")"

printf '\n\033[1mTenant isolation\033[0m\n'
check "recruiter reads their own tenant" 200 \
  "$(status -H "Authorization: Bearer $RECRUITER" "$BASE/tenants/$ORG/applications")"
check "recruiter is refused another tenant" 404 \
  "$(status -H "Authorization: Bearer $RECRUITER" "$BASE/tenants/$OTHER/applications")"
check "a user with no membership is refused" 404 \
  "$(status -H "Authorization: Bearer $OUTSIDER" "$BASE/tenants/$ORG/applications")"

printf '\n\033[1mRole separation\033[0m\n'
check "viewer reads applications" 200 \
  "$(status -H "Authorization: Bearer $VIEWER" "$BASE/tenants/$ORG/applications")"
check "viewer is denied call creation" 403 \
  "$(status -H "Authorization: Bearer $VIEWER" -X POST -H 'Content-Type: application/json' \
     -d '{"candidate_key":"strong_fit"}' "$BASE/tenants/$ORG/calls")"
check "recruiter may create a call" 200 \
  "$(status -H "Authorization: Bearer $RECRUITER" -X POST -H 'Content-Type: application/json' \
     -d '{"candidate_key":"strong_fit"}' "$BASE/tenants/$ORG/calls")"

printf '\n\033[1mPlatform admin and break-glass\033[0m\n'
check "platform admin without break-glass is refused" 404 \
  "$(status -H "Authorization: Bearer $ROOT_TOKEN" "$BASE/tenants/$ORG/applications")"
check "platform admin may open break-glass" 200 \
  "$(status -H "Authorization: Bearer $ROOT_TOKEN" -X POST -H 'Content-Type: application/json' \
     -d "$BG_OPEN" "$BASE/platform/break-glass")"
check "platform admin reads the tenant after break-glass" 200 \
  "$(status -H "Authorization: Bearer $ROOT_TOKEN" "$BASE/tenants/$ORG/applications")"
check "recruiter may not open break-glass" 404 \
  "$(status -H "Authorization: Bearer $RECRUITER" -X POST -H 'Content-Type: application/json' \
     -d "$BG_DENIED" "$BASE/platform/break-glass")"

printf '\n\033[1mInternal surface\033[0m\n'
check "the bundle endpoint needs the internal token" 401 \
  "$(status "$BASE/internal/opa/bundle.tar.gz")"

printf '\n\033[1mAudit trail\033[0m\n'
EVENTS="$(psql_tac "select count(*) from audit_events where event_type like 'break_glass%'")"
check "break-glass access was audited" yes \
  "$([ "${EVENTS:-0}" -ge 2 ] && echo yes || echo no)"

printf '\n%s\n' "-------------------------------------------------------------"
if [ "$fail" -eq 0 ]; then
  printf '\033[1;32m%d passed, %d failed\033[0m\n' "$pass" "$fail"
else
  printf '\033[1;31m%d passed, %d failed\033[0m\n' "$pass" "$fail"
fi

cat <<'EOF'

This covers the application layer only. Compose runs no gateway, so nothing
here exercises the edge: requests rejected before they reach the API, the
gateway's own JWT pre-check, OPA policy decisions or rate limiting. Those are
asserted by ./scripts/k3d-verify.sh and nowhere else.

The two layers also answer differently on purpose. A tenant the caller has no
membership on is 404 here and 403 at the gateway: OPA denies on the bundle
before the API can decide whether the tenant exists.
EOF
exit $((fail > 0))
