# Deployment

Identity, edge policy and gateway config for the API. See issue #3 for the design.

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

`plugins.envoy_ext_authz_grpc.dry-run` is `true` in `k8s/opa-config.yaml`. In that mode OPA evaluates and logs every decision without enforcing it. Leave it there until the decision log shows no unexpected denials, then set it to `false`.

The first enforcing deploy of edge authorization is the most likely outage in this design. Do not skip the dry run.

## Operating

- **Alert on bundle age.** An OPA that cannot reach the bundle endpoint keeps serving its last snapshot indefinitely and reports itself healthy. The readiness probe uses `/health?bundle=true`, which fails until a bundle has activated — that covers startup, not staleness afterwards. Add a staleness alert from the status logs.
- **`failOpen` is `false`.** If OPA is unreachable, requests are denied rather than admitted.
- **Staleness is bounded by the poll interval.** Up to ~20s after a revocation, OPA still believes a grant exists. Sensitive routes re-check `memberships` in Postgres for exactly this reason, so the window only affects low-risk reads.
- **Do not expose the API directly.** The HTTPRoute deliberately matches only `/tenants`; `/internal` is reachable in-cluster and must never be routed from the gateway.

## Untested here

The Kubernetes manifests are validated for structure (`kubectl kustomize deploy` builds, ConfigMap names propagate into the Deployment) but have not been applied to a live cluster. Envoy Gateway's `SecurityPolicy` and `BackendTrafficPolicy` field names have moved across releases — check them against the version you pin before the first apply.
