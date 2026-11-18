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

### Tokens must come from inside the Compose network

Keycloak's issuer is pinned to `http://keycloak:8080` via `KC_HOSTNAME`, so every token carries `iss: http://keycloak:8080/realms/aihr` — which is exactly what the API expects. A token fetched from the host against `localhost:8080` would carry a different issuer and is correctly rejected by every component.

```
TOKEN=$(make dev-token USER=recruiter)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/tenants/$ORG_ID/applications | jq
```

`make dev-token` runs `scripts/get_token.py` through `docker compose exec`, so it resolves `keycloak` by service name.

### Reaching the Keycloak admin console

The pinned hostname applies to the console too: Keycloak redirects `/admin/` to `http://keycloak:8080`, and its JavaScript calls the Admin API at that same origin. A browser that cannot resolve `keycloak` loads the page and then fails every call — the console shows an untranslated `somethingWentWrong`, because even its translations are fetched over that API.

Make the name resolve on the host, once per machine:

```
echo '127.0.0.1 keycloak' | sudo tee -a /etc/hosts
```

Then open **http://keycloak:8080** (`admin` / `admin`). Setting `KC_HOSTNAME_ADMIN` instead does not work: it moves the console's own URL but not the API base it calls, so the failure survives in a more confusing form.

With that entry in place `localhost` and `keycloak` are the same machine from the host's point of view, so a token fetched directly from `http://keycloak:8080` is also valid — `make dev-token` stops being the only option.

The realm in `deploy/dev/realm-aihr-local.json` is imported **only when the realm does not already exist**, and Keycloak keeps it in a volume. After editing that file, drop the volume or the change is silently ignored:

```
docker compose rm -sf keycloak keycloak-db && docker volume rm aihr_keycloakdata
```

### What `make dev-verify` does not cover

It asserts the **application layer only**: 401 without a token, tenant isolation, a viewer denied call creation, a platform admin refused until break-glass, and the audit rows that follow. Compose runs no gateway, so rejection at the edge and OPA's own decisions are untested by it — `./scripts/k3d-verify.sh` is the only thing that covers those.

The two layers answer differently on purpose. A tenant the caller has no membership on is **404** from the API and **403** through the gateway, because OPA denies on the bundle before the API can decide whether that tenant exists.

### The agent worker

`agent-worker` needs LiveKit, Deepgram and Cartesia credentials, so it sits behind a Compose profile and `make dev` skips it. Once the keys are in `.env`:

```
docker compose --profile agent up -d agent-worker
```

See `deploy/README.md` for the gateway, the OPA policy and how a request is authorized.
