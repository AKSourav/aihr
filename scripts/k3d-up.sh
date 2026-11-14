#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${CLUSTER:-aihr}"
NAMESPACE="${NAMESPACE:-aihr}"
GATEWAY_PORT="${GATEWAY_PORT:-8081}"
EG_VERSION="${EG_VERSION:-v1.2.6}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RECRUITER_SUB="11111111-1111-1111-1111-111111111111"
VIEWER_SUB="22222222-2222-2222-2222-222222222222"
ROOT_SUB="33333333-3333-3333-3333-333333333333"
OUTSIDER_SUB="44444444-4444-4444-4444-444444444444"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

for tool in docker k3d kubectl helm; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not installed"
done
docker info >/dev/null 2>&1 || die "docker daemon is not running"

say "Creating k3d cluster '$CLUSTER' (gateway on localhost:$GATEWAY_PORT)"
if k3d cluster list 2>/dev/null | grep -q "^$CLUSTER "; then
  echo "cluster already exists, reusing it"
else
  k3d cluster create "$CLUSTER" \
    --agents 1 \
    --port "$GATEWAY_PORT:80@loadbalancer" \
    --k3s-arg "--disable=traefik@server:*" \
    --wait
fi
kubectl config use-context "k3d-$CLUSTER" >/dev/null

say "Installing Envoy Gateway $EG_VERSION"
if kubectl get deployment -n envoy-gateway-system envoy-gateway >/dev/null 2>&1; then
  echo "envoy gateway already installed"
else
  helm install eg oci://docker.io/envoyproxy/gateway-helm \
    --version "$EG_VERSION" \
    -n envoy-gateway-system --create-namespace \
    --wait --timeout 10m
fi
kubectl wait --for=condition=Available --timeout=5m \
  -n envoy-gateway-system deployment/envoy-gateway

say "Building images"
docker build -q -f "$ROOT/services/api/Dockerfile" -t aihr-api:local "$ROOT"
docker build -q -f "$ROOT/services/temporal_worker/Dockerfile" -t aihr-worker:local "$ROOT"

say "Pre-pulling third-party images on the host"
THIRD_PARTY=(
  pgvector/pgvector:pg16
  postgres:16
  redis:7-alpine
  chainguard/minio:latest
  temporalio/temporal:latest
  quay.io/keycloak/keycloak:26.0
  openpolicyagent/opa:1.4.2-envoy
  "docker.io/envoyproxy/envoy:distroless-v1.32.3"
)
PULLED=()
for image in "${THIRD_PARTY[@]}"; do
  if docker pull -q "$image" >/dev/null 2>&1; then
    printf '  %s\n' "$image"
    PULLED+=("$image")
  else
    printf '  \033[1;33mskipped\033[0m %s (unavailable for %s; the cluster will try to pull it)\n' \
      "$image" "$(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null)"
  fi
done

say "Importing images into the cluster"
k3d image import aihr-api:local aihr-worker:local "${PULLED[@]}" -c "$CLUSTER"

say "Applying manifests"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f - <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy-gateway
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
EOF
kubectl kustomize --load-restrictor=LoadRestrictionsNone "$ROOT/deploy/local" | kubectl apply -f -

say "Waiting for infrastructure"
kubectl wait --for=condition=Available --timeout=5m -n "$NAMESPACE" \
  deployment/postgres deployment/keycloak-db deployment/redis deployment/minio deployment/temporal
kubectl wait --for=condition=Available --timeout=10m -n "$NAMESPACE" deployment/keycloak
kubectl wait --for=condition=Available --timeout=5m -n "$NAMESPACE" \
  deployment/aihr-api deployment/opa

say "Creating the artifact bucket"
kubectl exec -n "$NAMESPACE" deployment/minio -- \
  mc alias set local http://localhost:9000 minio minio12345 >/dev/null 2>&1 || true
kubectl exec -n "$NAMESPACE" deployment/minio -- \
  mc mb -p local/hr-artifacts >/dev/null 2>&1 || true

say "Running migrations and seed"
kubectl exec -n "$NAMESPACE" deployment/aihr-api -- alembic upgrade head
kubectl exec -n "$NAMESPACE" deployment/aihr-api -- python -m scripts.seed

say "Granting tenant access to the realm users"
kubectl exec -n "$NAMESPACE" deployment/aihr-api -- \
  python -m scripts.grant_membership --sub "$RECRUITER_SUB" --email recruiter@acme.test \
  --org "Acme Fintech" --role recruiter
kubectl exec -n "$NAMESPACE" deployment/aihr-api -- \
  python -m scripts.grant_membership --sub "$VIEWER_SUB" --email viewer@acme.test \
  --org "Acme Fintech" --role viewer
kubectl exec -n "$NAMESPACE" deployment/aihr-api -- \
  python -m scripts.grant_membership --sub "$ROOT_SUB" --email root@aihr.test --platform-admin
kubectl exec -n "$NAMESPACE" deployment/aihr-api -- \
  python -m scripts.grant_membership --sub "$OUTSIDER_SUB" --email outsider@nowhere.test

say "Waiting for the gateway to be programmed"
kubectl wait --for=condition=Programmed --timeout=5m -n "$NAMESPACE" gateway/aihr

ORG_ID="$(kubectl exec -n "$NAMESPACE" deployment/postgres -- \
  psql -U app -d hr -tAc "select id from organizations where name = 'Acme Fintech'" | tr -d '[:space:]')"

cat <<EOF

$(printf '\033[1;32mCluster is up.\033[0m')

  Gateway       http://localhost:$GATEWAY_PORT
  Tenant        $ORG_ID
  Namespace     $NAMESPACE

  Users (password == username, client aihr-local-cli):
    recruiter   recruiter on Acme Fintech
    viewer      viewer on Acme Fintech
    root        platform admin, no membership
    outsider    no access at all

  Get a token and call through the gateway:
    TOKEN=\$(kubectl exec -n $NAMESPACE deployment/aihr-api -- \\
      python -m scripts.get_token --username recruiter --password recruiter)
    curl -s -H "Authorization: Bearer \$TOKEN" \\
      http://localhost:$GATEWAY_PORT/tenants/$ORG_ID/applications | jq

  Verify the whole path:  ./scripts/k3d-verify.sh
  Tear down:              ./scripts/k3d-down.sh
EOF
