#!/usr/bin/env bash
set -uo pipefail

CLUSTER="${CLUSTER:-aihr}"
NAMESPACE="${NAMESPACE:-aihr}"
GATEWAY_PORT="${GATEWAY_PORT:-8081}"
BASE="http://localhost:$GATEWAY_PORT"

pass=0
fail=0

token_for() {
  kubectl exec -n "$NAMESPACE" deployment/aihr-api -- \
    python -m scripts.get_token --username "$1" --password "$1" 2>/dev/null
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

ORG="$(kubectl exec -n "$NAMESPACE" deployment/postgres -- \
  psql -U app -d hr -tAc "select id from organizations where name = 'Acme Fintech'" | tr -d '[:space:]')"
OTHER="00000000-0000-0000-0000-0000000000ff"

BG_OPEN="{\"org_id\":\"$ORG\",\"reason\":\"verification run from k3d-verify\"}"
BG_DENIED="{\"org_id\":\"$ORG\",\"reason\":\"should not be permitted\"}"

RECRUITER="$(token_for recruiter)"
VIEWER="$(token_for viewer)"
ROOT_TOKEN="$(token_for root)"
OUTSIDER="$(token_for outsider)"

[ -n "$RECRUITER" ] || { echo "could not obtain a token; is the cluster up?"; exit 1; }

printf '\n\033[1mEdge authentication\033[0m\n'
check "no token is rejected at the gateway" 401 \
  "$(status "$BASE/tenants/$ORG/applications")"
check "a garbage token is rejected at the gateway" 401 \
  "$(status -H 'Authorization: Bearer not.a.jwt' "$BASE/tenants/$ORG/applications")"

printf '\n\033[1mTenant isolation\033[0m\n'
check "recruiter reads their own tenant" 200 \
  "$(status -H "Authorization: Bearer $RECRUITER" "$BASE/tenants/$ORG/applications")"
check "recruiter is denied another tenant" 403 \
  "$(status -H "Authorization: Bearer $RECRUITER" "$BASE/tenants/$OTHER/applications")"
check "a user with no membership is denied" 403 \
  "$(status -H "Authorization: Bearer $OUTSIDER" "$BASE/tenants/$ORG/applications")"

printf '\n\033[1mRole separation\033[0m\n'
check "viewer reads scores" 200 \
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

printf '\n\033[1mInternal surface is not routed\033[0m\n'
check "internal bundle endpoint is unreachable via gateway" 404 \
  "$(status "$BASE/internal/opa/bundle.tar.gz")"

printf '\n\033[1mAudit trail\033[0m\n'
EVENTS="$(kubectl exec -n "$NAMESPACE" deployment/postgres -- psql -U app -d hr -tAc \
  "select count(*) from audit_events where event_type like 'break_glass%'" | tr -d '[:space:]')"
check "break-glass access was audited" yes "$([ "${EVENTS:-0}" -ge 2 ] && echo yes || echo no)"

printf '\n%s\n' "-------------------------------------------------------------"
if [ "$fail" -eq 0 ]; then
  printf '\033[1;32m%d passed, %d failed\033[0m\n' "$pass" "$fail"
else
  printf '\033[1;31m%d passed, %d failed\033[0m\n' "$pass" "$fail"
fi
exit $((fail > 0))
