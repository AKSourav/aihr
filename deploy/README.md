# Deployment

Identity, edge policy and gateway config for the API. See issue #3 for the design.

## Running it locally

Docker Compose runs the application — including real login and real role enforcement — but **not** the gateway: Envoy Gateway is a Kubernetes controller that generates Envoy config from Gateway API CRDs, so there is nothing for it to do without a cluster. Compose is the inner loop (`make dev`); use k3d to exercise the real request path.

```
./scripts/k3d-up.sh        # cluster, Envoy Gateway, the whole stack, migrated and seeded
./scripts/k3d-verify.sh    # asserts the authz behaviour end to end through the gateway
./scripts/k3d-down.sh      # delete the cluster
```

`deploy/local/` is a kustomize overlay over the **same** `k8s/` manifests this directory ships, patched for local use: HTTP listener instead of HTTPS, in-cluster Keycloak issuer, and a local rate limit instead of the global one (which needs a separate ratelimit service). The overlay builds with `--load-restrictor=LoadRestrictionsNone` because it reads `../opa/policy.rego` and `../k8s/*` from outside its own root.

`deploy/dev/realm-aihr-local.json` adds what production must never have: an `aihr-local-cli` client with the password grant enabled, and four fixed-ID users so memberships can be granted deterministically. It sits outside both `local/` and `keycloak/` because Compose and k3d share it — Compose mounts the file, kustomize generates a ConfigMap from it.

| User | Password | Access |
| --- | --- | --- |
| `owner` | `owner` | account owner on Acme Group |
| `recruiter` | `recruiter` | recruiter on Acme Fintech |
| `viewer` | `viewer` | viewer on Acme Fintech |
| `root` | `root` | platform admin, no membership |
| `outsider` | `outsider` | none |

### Known image constraints

Both of these were hit while bringing the cluster up, and both are about image availability rather than configuration:

- **Use the `-static` OPA tags, not the default ones.** `openpolicyagent/opa:1.4.2-envoy` publishes **only** `linux/amd64`, so on Apple Silicon the image cannot run — the k3d nodes carry no qemu binfmt, so emulation is not a fallback. `openpolicyagent/opa:1.4.2-envoy-static` is multi-arch (`amd64` + `arm64`), has the same `/opa` entrypoint, and still starts the `envoy_ext_authz_grpc` plugin. CI uses `1.4.2-static` for `opa check` and `opa test` for the same reason, so the same command runs natively on either architecture.
- **`minio/minio` is no longer pullable** — Docker Hub reports `pull access denied` and lists no tags, and `quay.io/minio/minio` is unavailable too. The local overlay therefore uses `chainguard/minio`, which is a drop-in: same `/usr/bin/minio` entrypoint, same arguments, and it still ships `mc` for bucket creation. It runs as UID 65532, hence the `fsGroup` on the pod. `docker-compose.yml` had the same problem on both `minio` and `minio-init` (which used `minio/mc:latest`) and now uses the same image; `mc` ships inside it, so the bucket and retention setup is unchanged.

`agent-worker` is deliberately not deployed — it needs LiveKit, Deepgram and Cartesia credentials and would crashloop without them. Compose keeps it behind the `agent` profile for the same reason. Everything the auth path touches is there.

The admin console inherits the pinned hostname — it redirects to `http://keycloak:8080` and calls the Admin API there — so reaching it needs `127.0.0.1 keycloak` in the host's `/etc/hosts`, plus `kubectl port-forward -n aihr svc/keycloak 8080:8080` under k3d. `KC_HOSTNAME_ADMIN` does not substitute: it relocates the console but not the API base it calls.

Keycloak's issuer is pinned to `http://keycloak:8080` via `KC_HOSTNAME`. Tokens are therefore minted with `iss: http://keycloak:8080/realms/aihr`, which is what the API expects and what Envoy validates, so tokens must be requested from inside the cluster — `scripts/get_token.py` does that through `kubectl exec`. Requesting one from the host against a different hostname would produce a token every component correctly rejects.

## Layout

| Path | What it is |
| --- | --- |
| `keycloak/realm-aihr.json` | The deployable realm: clients and roles, no users and no password grant |
| `dev/realm-aihr-local.json` | The local realm, shared by Compose and k3d: adds the CLI client and four fixed-ID users |
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

The bundle declares `roots` limited to the data keys it owns (`memberships`, `account_access`, `platform_admins`, `role_permissions`, `routes`, `platform_routes`). Without that, bundle activation would claim the whole document and evict the policy loaded from the ConfigMap.

Route entries carry a `scope` of `tenant` or `account`. The tenant rule resolves the path's capture group against `memberships`; the account rule resolves it against `account_access`, which lists the accounts a subject may touch — account owners, plus tenant admins mapped through their org's account. The guard matters: without it an account route would be satisfied by a tenant membership whose `org_id` matched the account id in the path. The edge deliberately stops there. It answers "may this subject touch this account at all", never which org may be granted — those ids are in the request body, not the path, and the API re-reads every premise under lock before writing.

Route-to-permission mapping lives in `packages/core/permissions.py` and is exported into the bundle. The API and the policy therefore read the same table, and `tests/unit/test_permissions.py` fails if a tenant-scoped route exists without a permission, or a mapped route is not served.

## Rolling out

`plugins.envoy_ext_authz_grpc.dry-run` is `false` in `k8s/opa-config.yaml` — the committed state is the steady state, enforcing.

**For the first deploy into any environment, set it to `true` first.** In that mode OPA evaluates and logs every decision without acting on it. Watch the decision log until no unexpected denials appear, then set it back to `false` and redeploy.

The first enforcing deploy of edge authorization is the most likely outage in this design. Do not skip the dry run.

## Account administration

`aihr-admin` is a confidential service-account client holding `realm-management`'s `manage-users` and `view-users`. The invitation route uses it to create the Keycloak identity **before** committing local rows, so a failure there cannot leave a membership pointing at a subject that does not exist — the API answers 502 and writes nothing.

The entitlement decision is made twice on purpose. `account_context` resolves the caller before the handler runs and is advisory; `authorize_grants()` re-reads every premise inside the write transaction under `FOR SHARE`, because a membership can be revoked in between. Locks are taken organizations → users → memberships so a concurrent revoke cannot deadlock against it.

One predicate carries the isolation property: every `org_id` in the body must come back from `SELECT id FROM organizations WHERE account_id = :account AND id = ANY(:ids)`. An id that is missing is refused without saying whether it exists elsewhere — distinguishing the two would enumerate another customer's tenants.

`execute-actions-email` needs SMTP. The dev realm points at Mailpit (`http://localhost:8025`); without a mail server the invitation still succeeds and reports `invitation_email_sent: false` rather than failing.

## Public and self routes

Two kinds of route sit outside the per-tenant permission table:

- **`GET /me`** returns the caller's memberships and permissions. The desktop app uses it to pick an org and to hide actions the caller's role lacks. It lives in `SELF_ROUTES` in `packages/core/permissions.py` and is exported into the bundle as `self_routes`. OPA admits any verified subject to it. The response only ever describes the caller.
- **`/join/{token}` and `/public/invites/{token}[/join]`** are the candidate's email-invite pages. They are served on a separate `aihr-public` HTTPRoute that the `aihr-authn` SecurityPolicy does not target, because candidates have no Keycloak account. The credential is the invite token: a JWT signed with `INVITE_SIGNING_KEY`, scoped to one call, and expiring with the invite. Calling `join` creates the LiveKit room and dispatches the agent the first time it succeeds; reloads rejoin the same room. The route carries the same rate limit as the API.

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
