# aihr

## Local development

Two environments. Compose is the one you use all day; k3d is for the edge.

| | Compose | k3d |
| --- | --- | --- |
| App, Postgres, Temporal, MinIO, Keycloak | yes | yes |
| Authentication — the API verifies the JWT itself | yes | yes |
| Authorization — memberships, roles, break-glass, audit | yes | yes |
| OPA edge policy, gateway JWT pre-check, rate limiting | no | yes |
| Start-up cost | seconds | minutes |

Reach for k3d when changing `deploy/opa/policy.rego`, the gateway manifests, or anything else about the edge. Everything else belongs in Compose.

```
cp .env.example .env
make dev            # up, migrate, seed, grant the dev users, print the tenant id
make dev-verify     # assert the application-layer authz behaviour
make down
```

`make dev` prints the tenant id and the four realm users. Passwords equal usernames:

| User | Access |
| --- | --- |
| `recruiter` | recruiter on Acme Fintech |
| `viewer` | viewer on Acme Fintech |
| `root` | platform admin, no membership |
| `outsider` | none |

### Two names for one Keycloak

Containers reach Keycloak at `http://keycloak:8080`; everything outside Docker reaches it at `http://localhost:8080`. The host cannot resolve `keycloak` — it is a name in Docker's internal DNS, and on macOS the containers live in a VM the host has no route into. No Compose network setting changes that.

So the two names are kept apart deliberately:

| Setting | Value | Who uses it |
| --- | --- | --- |
| `KC_HOSTNAME` | `http://localhost:8080` | what Keycloak stamps into every token as `iss`, and the base its own pages redirect to |
| `KEYCLOAK_ISSUER_URL` | `http://localhost:8080` | the issuer the API requires — must equal `KC_HOSTNAME` |
| `KEYCLOAK_URL` | `http://keycloak:8080` | where the API fetches signing keys, over the Compose network |

A token therefore says `localhost:8080` no matter where it was requested from, so browsers, the desktop app and `make dev-token` all produce tokens the API accepts.

```
TOKEN=$(make dev-token USER=recruiter)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/tenants/$ORG_ID/applications | jq
```

Change `KC_HOSTNAME` without changing `KEYCLOAK_ISSUER_URL` and every token is rejected — that is the one way to break this.

The admin console is at **http://localhost:8080** (`admin` / `admin`).

The realm in `deploy/dev/realm-aihr-local.json` is imported **only when the realm does not already exist**, and Keycloak keeps it in a volume. After editing that file, drop the volume or the change is silently ignored:

```
docker compose rm -sf keycloak keycloak-db && docker volume rm aihr_keycloakdata
```

### What `make dev-verify` does not cover

It asserts the **application layer only**: 401 without a token, tenant isolation, a viewer denied call creation, a platform admin refused until break-glass, and the audit rows that follow. Compose runs no gateway, so rejection at the edge and OPA's own decisions are untested by it — `./scripts/k3d-verify.sh` is the only thing that covers those.

The two layers answer differently on purpose. A tenant the caller has no membership on is **404** from the API and **403** through the gateway, because OPA denies on the bundle before the API can decide whether that tenant exists.

### Inviting people

Anyone may create their own organization: `POST /signup` makes the Keycloak identity, the account, the organization and an owner membership, and Keycloak emails a set-password link. `POST /organizations` does the same for someone who already has an identity but no memberships.

`make dev` still bootstraps its owner and platform admin with `scripts/grant_membership.py`, because those two need the Keycloak subjects the realm import fixed in advance, then invites the recruiter and the viewer **through the API**. `is_platform_admin` is deliberately not settable over HTTP.

```bash
ACCOUNT=$(docker compose exec -T postgres psql -U app -d hr -tAc \
  "select id from accounts where name='Acme Group'")
TOKEN=$(make dev-token USER=owner)

curl -s -X POST http://localhost:8000/accounts/$ACCOUNT/invitations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"priya@acme.test","memberships":[{"org_id":"'"$ORG_ID"'","role":"recruiter"}]}' | jq
```

That creates the Keycloak identity, attaches the user to the account, grants the memberships, and sends a set-password mail. Read it at **http://localhost:8025** — Mailpit catches everything; nothing leaves the machine.

| | |
|---|---|
| `POST /accounts/{id}/invitations` | invite, with roles on several tenants at once |
| `GET /accounts/{id}/members` | members and their memberships |
| `PATCH /accounts/{id}/members/{user_id}` | replace a member's membership set |
| `DELETE /accounts/{id}/members/{user_id}` | revoke every membership in the account |
| `POST /accounts/{id}/tenants` | create a tenant — account owner only |

Account owners may act on every tenant under their account; tenant admins only on the tenants they administer. A recruiter or viewer gets 404 on all of it, as does a platform admin — break-glass is org-scoped and deliberately does not reach here.

### The agent worker

`agent-worker` needs LiveKit, Deepgram and Cartesia credentials, so it sits behind a Compose profile and `make dev` skips it. Once the keys are in `.env`:

```
docker compose --profile agent up -d agent-worker
```

See `deploy/README.md` for the gateway, the OPA policy and how a request is authorized.
