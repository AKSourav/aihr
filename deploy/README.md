# Deployment

Identity, edge policy and gateway config for the API. See issue #3 for the design.

## Running it locally

Docker Compose runs the application but **not** the gateway: Envoy Gateway is a Kubernetes controller that generates Envoy config from Gateway API CRDs, so there is nothing for it to do without a cluster. To exercise the real request path, use k3d.

```
./scripts/k3d-up.sh        # cluster, Envoy Gateway, the whole stack, migrated and seeded
./scripts/k3d-verify.sh    # asserts the authz behaviour end to end through the gateway
./scripts/k3d-down.sh      # delete the cluster
```

`deploy/local/` is a kustomize overlay over the **same** `k8s/` manifests this directory ships, patched for local use: HTTP listener instead of HTTPS, in-cluster Keycloak issuer, and a local rate limit instead of the global one (which needs a separate ratelimit service). The overlay builds with `--load-restrictor=LoadRestrictionsNone` because it reads `../opa/policy.rego` and `../k8s/*` from outside its own root.

`deploy/local/realm-aihr-local.json` adds what production must never have: an `aihr-local-cli` client with the password grant enabled, and four fixed-ID users so memberships can be granted deterministically.

| User | Password | Access |
| --- | --- | --- |
| `recruiter` | `recruiter` | recruiter on Acme Fintech |
| `viewer` | `viewer` | viewer on Acme Fintech |
| `root` | `root` | platform admin, no membership |
| `outsider` | `outsider` | none |

### Known image constraints

Both of these were hit while bringing the cluster up, and both are about image availability rather than configuration:

- **OPA's ext_authz image is amd64-only.** `openpolicyagent/opa:1.4.2-envoy` publishes no `linux/arm64` manifest, so on Apple Silicon the host pre-pull skips it and the k3d node cannot run it either (the nodes carry no qemu binfmt). On arm64 you need a multi-arch OPA build, or an amd64 cluster. `k3d-up.sh` warns and continues rather than aborting.
- **`minio/minio` is no longer pullable** — Docker Hub reports `pull access denied` and lists no tags, and `quay.io/minio/minio` is unavailable too. The local overlay therefore uses `chainguard/minio`, which is a drop-in: same `/usr/bin/minio` entrypoint, same arguments, and it still ships `mc` for bucket creation. It runs as UID 65532, hence the `fsGroup` on the pod. **`docker-compose.yml` still pins `minio/minio:latest` and will fail the same way on a cold cache.**

`agent-worker` is deliberately not deployed — it needs LiveKit, Deepgram and Cartesia credentials and would crashloop without them. Everything the auth path touches is there.

Keycloak's issuer is pinned to `http://keycloak:8080` via `KC_HOSTNAME`. Tokens are therefore minted with `iss: http://keycloak:8080/realms/aihr`, which is what the API expects and what Envoy validates, so tokens must be requested from inside the cluster — `scripts/get_token.py` does that through `kubectl exec`. Requesting one from the host against a different hostname would produce a token every component correctly rejects.

## Layout

| Path | What it is |
| --- | --- |
| `keycloak/realm-aihr.json` | Realm import, loaded by the compose `keycloak` service and reproducible in any environment |
| `opa/policy.rego` | The authorization policy. Single source — kustomize generates the ConfigMap from this file |
| `opa/policy_test.rego` | Policy tests, run by `opa test` in CI |
| `k8s/opa.yaml` | OPA deployment and service, exposing ext_authz on gRPC 9191 |
| `k8s/opa-config.yaml` | Bundle polling, ext_authz plugin, decision logging |
| `k8s/gateway.yaml` | Gateway, HTTPRoute, SecurityPolicy (JWT + extAuth), BackendTrafficPolicy (rate limit) |
| `kustomization.yaml` | Ties it together; run `kubectl kustomize deploy` |

## How a request is authorized

1. Envoy validates the Keycloak JWT and copies the `sub` claim into `x-jwt-sub`.
2. Envoy calls OPA over ext_authz. OPA matches method and path against `data.routes`, extracts the tenant from the path's capture group, and checks the caller's role in that tenant against `data.role_permissions`.
3. Envoy forwards the request to the API **with the original JWT intact**.
4. The API verifies the JWT again and applies `WHERE org_id = :org_id` to every query.

Steps 3 and 4 are not redundant. `agent-worker` and `temporal-worker` reach the API directly over cluster networking and never traverse the gateway, so an injected identity header would be forgeable by anything that can reach the Service. The API must never trust a header it cannot verify itself.

Edge policy authorizes *the path*; the `org_id` predicate authorizes *the row*. OPA knows the caller holds a membership on the tenant named in the URL, but has no idea which tenant a given `call_id` actually belongs to. Only the query predicate closes that.

## Policy data

OPA never receives a write. The API serves `GET /internal/opa/bundle.tar.gz` — built from Postgres, behind the internal token — and OPA polls it every 10–20s. Postgres stays the only source of truth, so the two cannot diverge on content, only on recency.

The bundle declares `roots` limited to the data keys it owns (`memberships`, `account_owners`, `platform_admins`, `role_permissions`, `routes`). Without that, bundle activation would claim the whole document and evict the policy loaded from the ConfigMap.

Route-to-permission mapping lives in `packages/core/permissions.py` and is exported into the bundle. The API and the policy therefore read the same table, and `tests/unit/test_permissions.py` fails if a tenant-scoped route exists without a permission, or a mapped route is not served.

## Rolling out

`plugins.envoy_ext_authz_grpc.dry-run` is `false` in `k8s/opa-config.yaml` — the committed state is the steady state, enforcing.

**For the first deploy into any environment, set it to `true` first.** In that mode OPA evaluates and logs every decision without acting on it. Watch the decision log until no unexpected denials appear, then set it back to `false` and redeploy.

The first enforcing deploy of edge authorization is the most likely outage in this design. Do not skip the dry run.

## Break-glass

`is_platform_admin` does **not** grant tenant data access on its own. A platform admin with no membership gets 404 on every tenant route, exactly like any other stranger.

To read tenant data, open a scope first:

```
POST /platform/break-glass
  { "org_id": "...", "reason": "debugging call timeout #412", "minutes": 60 }
```

That writes a `break_glass_opened` audit event, and every subsequent request made under the scope writes a `break_glass_access` event naming the permission used. Scopes are bounded to 4 hours, are specific to one operator and one tenant, and expire without needing revocation.

Break-glass is never evaluated at the edge. OPA lets platform admins past the gateway on tenant routes; the API is what demands an open scope. Short-lived, time-boxed state is the worst possible fit for a polled bundle, so it stays where it can be read transactionally.

## Operating

- **Alert on bundle age.** An OPA that cannot reach the bundle endpoint keeps serving its last snapshot indefinitely and reports itself healthy. The readiness probe uses `/health?bundle=true`, which fails until a bundle has activated — that covers startup, not staleness afterwards. Add a staleness alert from the status logs.
- **`failOpen` is `false`.** If OPA is unreachable, requests are denied rather than admitted.
- **Staleness is bounded by the poll interval.** Up to ~20s after a revocation, OPA still believes a grant exists. Sensitive routes re-check `memberships` in Postgres for exactly this reason, so the window only affects low-risk reads.
- **Do not expose the API directly.** The HTTPRoute deliberately matches only `/tenants`; `/internal` is reachable in-cluster and must never be routed from the gateway.

## Untested here

The Kubernetes manifests are validated for structure (`kubectl kustomize deploy` builds, ConfigMap names propagate into the Deployment) but have not been applied to a live cluster. Envoy Gateway's `SecurityPolicy` and `BackendTrafficPolicy` field names have moved across releases — check them against the version you pin before the first apply.
