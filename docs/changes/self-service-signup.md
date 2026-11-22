# Self-service signup: move tenancy off the user row

Branch: `issue-17-self-service-signup`, based on `main` at `a7adab4` ("Recruiter desktop app and email screening invites (#15)").
Status: **merged through [#18](https://github.com/AKSourav/aihr/pull/18).** Closes [#17](https://github.com/AKSourav/aihr/issues/17).

This document covers what the branch changes, the design decision behind it, the two latent bugs it exposes, how it was tested, and what is still open.

## Contents

1. [Summary](#1-summary)
2. [What existed before, and why signup was impossible](#2-what-existed-before-and-why-signup-was-impossible)
3. [The data model change](#3-the-data-model-change)
4. [Signup and organization creation](#4-signup-and-organization-creation)
5. [Authorization changes](#5-authorization-changes)
6. [Edge and routing](#6-edge-and-routing)
7. [Migration 0005](#7-migration-0005)
8. [Desktop app](#8-desktop-app)
9. [Bugs exposed and fixed](#9-bugs-exposed-and-fixed)
10. [Backward compatibility](#10-backward-compatibility)
11. [Testing](#11-testing)
12. [Known gaps and follow-ups](#12-known-gaps-and-follow-ups)
13. [File inventory](#13-file-inventory)

---

## 1. Summary

| | Before | After |
| --- | --- | --- |
| Becoming a customer | `scripts/seed.py` only, hardcoded to "Acme Group" | `POST /signup`, unauthenticated |
| Where tenancy lives | `users.account_id` + `users.account_role` | `memberships.role`, nothing on `users` |
| Accounts per person | exactly one, for life | any number, with a different role in each |
| `owner` | an account-level role, no tenant permissions | a membership role beside admin, recruiter, viewer |
| A user with no memberships | broken: 404 from every account route | ordinary: `/me` returns an empty list |
| Creating your own tenant | impossible | `POST /organizations` |
| Account name | unique across the install | free to repeat |

Size: 28 tracked files changed (+822 / −175), roughly half of it tests.

Three questions were settled before implementation:

| Question | Answer |
| --- | --- |
| Registration | `POST /signup` on our API, not Keycloak's hosted registration page |
| Organizations per identity | Unlimited |
| `accounts` table | Kept, as a dormant billing parent |

## 2. What existed before, and why signup was impossible

`main` already had multi-tenant auth from #3 and the account administration API from #10: invitations through the Keycloak Admin API, membership management, and tenant creation under an existing account.

What it did not have was any way to get the *first* account. `Account` was the only entity with no API surface at all — `scripts/seed.py:152` created one and nothing else ever did.

The blocker was not a missing endpoint. It was that `users` carried `account_id` and `account_role`, so tenancy lived on the identity:

- `account_context` resolved the caller's account from `users.account_id`. A user with no account resolved to nothing and got a 404 from every account route.
- A person belonged to exactly one account permanently. A recruiter could not work for two companies, or move between them.
- The only two writers of those columns were the seed script and `POST /accounts/{account_id}/invitations` — and the invite path needs a caller who already has them.

Adding `POST /platform/accounts` behind `platform_admin` would have papered over this: a human gatekeeper on every signup, and the one-account-per-person limit untouched.

## 3. The data model change

Tenancy moves off the identity and onto the membership — the flat workspace model that Slack, Linear, Notion and Vercel use.

```
users ──1:N──▶ memberships ──N:1──▶ organizations ──N:1──▶ accounts
                   role                                  (billing parent,
            owner│admin│recruiter│viewer                  dormant)
```

| Table | Change |
| --- | --- |
| `users` | `account_id` and `account_role` dropped |
| `memberships` | `role` check constraint widened to include `owner` |
| `organizations` | unchanged |
| `accounts` | `name` loses its unique constraint |

`accounts` stays, and one is created per signup, 1:1 with the first organization. That keeps `organizations.account_id` `NOT NULL`, keeps `/accounts/{account_id}/*` and `account_context` working, and leaves room for one customer to hold several organizations under one contract. It carries no authorization meaning of its own any more.

Its name stops being unique because an account is now created by a signup rather than named by an operator — two customers may both be called "Acme".

### Zero memberships is a valid state

Before, a user with no memberships was a half-provisioned user: `account_context` could not resolve anything, so JIT provisioning (`services/api/auth.py:70`), console-created identities and federated IdPs all landed in a hole.

After, it is an ordinary state with an ordinary answer. `/me` returns an empty list and the client offers to create an organization.

This also shrinks [#9](https://github.com/AKSourav/aihr/issues/9): deprovisioning becomes "delete the memberships", which puts a user into a state the system already handles correctly.

## 4. Signup and organization creation

### `POST /signup` — unauthenticated

`services/api/routes/signup.py`. The only way to become a customer.

```json
{ "email": "priya@acme.com", "organization_name": "Acme Fintech" }
→ 202 { "status": "verification_sent" }
```

It creates the Keycloak identity, the account, the organization and an owner membership in **one transaction**, then has Keycloak email a set-password link. The row grants nothing until that password is set — Keycloak will not mint a token for an account that cannot authenticate.

Creating everything synchronously is possible because `keycloak_admin.create_user` returns the `sub` immediately. This is exactly what `POST /accounts/{id}/invitations` already does, so there is no pending-signup state to store or reconcile.

| Case | Response |
| --- | --- |
| Fresh address | 202, everything created, email sent |
| Address already a local user | 202, nothing created |
| Address known only to Keycloak | 202, nothing created |
| Keycloak unreachable | 502, no local state |
| Missing or malformed field | 422 |

A known address returns the **same** 202 as a fresh one, so the endpoint cannot be used to discover who has an account.

### `POST /organizations` — authenticated

`services/api/routes/organizations.py`. The same thing for someone who already has an identity but no memberships — a JIT-provisioned user, or one created in the Keycloak console.

```json
{ "name": "Acme Fintech" }
→ 201 { "org_id": "…", "name": "Acme Fintech", "account_id": "…", "role": "owner" }
```

Both endpoints share `found_organization()`, which is the whole of the account/organization/membership creation.

There is no 409 on a duplicate organization name: each signup gets its own account and `organizations` is unique on `(account_id, name)`.

## 5. Authorization changes

**The Rego is unchanged.** `data.account_access` and `data.platform_admins` keep their shape; only the derivation moves.

| File | Change |
| --- | --- |
| `services/api/bundle.py` | `account_access` derives from `owner`/`admin` memberships joined to `organizations.account_id`, not from `user.account_role == "owner"` |
| `services/api/account_authz.py` | `account_context` and `authorize_grants` resolve `is_owner` from an owner membership. The `with_for_update(read=True)` lock moves from `users` to `memberships` |
| `packages/core/permissions.py` | `owner` joins `ROLE_PERMISSIONS` with the admin set plus `TENANT_CREATE`. `ACCOUNT_ROLE_PERMISSIONS`, `account_grants()` and `MEMBER` are deleted |
| `services/api/routes/account.py` | `account_role` leaves `InvitationRequest` and `MemberOut`. `TenantRole` gains `owner`. Granting `owner` requires `context.is_owner` |
| `services/api/authz.py` | unchanged — `tenant_context` already read `memberships` |

"Member of an account" changes meaning: it was `users.account_id == account_id`, it is now "holds at least one membership in an organization under that account". `list_members` and `member_of_account` join through `memberships` accordingly.

### Two enforcement points, different freshness

The gateway asks OPA, which polls the bundle every 10–20 s (`deploy/k8s/opa-config.yaml`). The API enforces from Postgres directly, with no staleness.

A deploy that ships the migration **before** the bundle code will deny account routes to owners for up to one poll interval, because the bundle still derives `account_access` from a column that no longer exists. Ship them together.

## 6. Edge and routing

`deploy/k8s/gateway.yaml`:

| Path | Route | Why |
| --- | --- | --- |
| `/organizations` | `aihr-api` (exact) | Authenticated; needs JWT and OPA |
| `/signup` | `aihr-public` (exact) | Unauthenticated by definition — nobody signing up has a Keycloak account yet |

`/organizations` also joins `SELF_ROUTES` in `packages/core/permissions.py`. The Rego defaults to deny and reports `"no matching route"`, so an endpoint absent from every route table is unreachable through the gateway regardless of what the handler does.

`/signup` needs **no** Rego entry: `aihr-public` sits outside both `aihr-authn` and the ext_authz filter. It inherits the existing `BackendTrafficPolicy` — 120 requests/minute per distinct source IP — which already targets that route.

## 7. Migration 0005

`migrations/versions/0005_tenancy_on_memberships.py`. In order:

1. Widen `ck_memberships_role` to `('owner', 'admin', 'recruiter', 'viewer')`
2. Promote existing memberships to `owner` where the user was an account owner of that organization's account
3. Insert `owner` memberships for every organization under the account where none existed
4. Drop `users.account_role` and `users.account_id`
5. Drop `accounts_name_key`

`downgrade()` restores both columns and the unique constraint, repopulates `account_role`/`account_id` from owner memberships, demotes `owner` memberships to `admin`, and narrows the check constraint back. Verified round-tripping `base → head → base → head` on an empty database.

**The downgrade fails if two accounts share a name**, which is precisely what the upgrade starts permitting:

```
duplicate key value violates unique constraint "accounts_name_key"
[SQL: ALTER TABLE accounts ADD CONSTRAINT accounts_name_key UNIQUE (name)]
```

Alembic runs it in a transaction, so it rolls back whole and leaves the schema at `0005` — there is no partial state. An operator who needs to go back must rename the duplicates first. The migration deliberately does not rename them itself: silently rewriting customer-visible account names during a rollback is worse than stopping.

Two further notes:

- Seed users carry `keycloak_sub LIKE 'seed:%'` (see migration `0003`); the backfill covers them by account, so `make seed` keeps producing a usable owner.
- An account owner whose account had **no** organizations gets no membership and therefore no access. That is consistent with the new model — they can create an organization — but it is a real behaviour change for an empty account.

## 8. Desktop app

Two screens, no new dependencies.

**Signup**, on the login card (`index.html`, `app.js`). A toggle swaps between "Sign in with Keycloak" and an email + organization-name form. On success the form is replaced by a toast directing the user to their email.

This needed a new IPC channel: `apiRequest` in `main.js` returns 401 when there is no token, which is correct for every other call and wrong for this one. `main.js` now has a shared `send()` helper, with `apiRequest` attaching a token and `signupRequest` not. `preload.js` exposes `signup(body)` alongside `get`/`post`. The renderer still never sees a token and still cannot name an arbitrary path on the signup channel.

**Create organization**, replacing the zero-membership empty state. It used to read "You don't have access to any organization yet. Ask an admin to grant you a role." It is now a form that calls `POST /organizations`, re-reads `/me`, repopulates the organization picker and routes into the new tenant.

Styling reuses the existing `.input`, `.btn`, `.field-label` and `.login-card` classes; the only CSS added is a two-line `.form` grid.

## 9. Bugs exposed and fixed

Both were latent — correct only while a user could belong to exactly one account, which this branch stops being true.

**`revoke_memberships` deleted memberships in other accounts.** It ran `delete(Membership).where(Membership.user_id == target.id)` with no account scope. An owner revoking a consultant from their account would have removed that person's access to every other customer's organizations too. Now scoped to organizations under the account being administered.

**`replace_memberships` read the target's memberships unscoped.** `current` included organizations from other accounts, so `touched` did too, and `authorize_grants` would have rejected the request with a 403 because those organizations do not belong to the caller's account. Both now go through `memberships_in_account()`.

A third, smaller one: the self-demotion guard checked only `ADMIN`, so an owner could demote themselves out of their own account. It now covers `owner` as well.

## 10. Backward compatibility

**Breaking, within the API:**

- `InvitationRequest.account_role` is gone. Owner is granted as a membership role: `{"memberships": [{"org_id": "…", "role": "owner"}]}`.
- `MemberOut.account_role` is gone from every account response.
- `scripts/grant_membership.py` loses `--account` and `--account-role`; `--role` gains `owner`. `scripts/dev-up.sh` and `scripts/k3d-up.sh` updated to `--org "Acme Fintech" --role owner`.

**Not breaking:**

- The Rego, the bundle's JSON shape, and every tenant route are untouched.
- `GET /me` keeps its shape; `owner` simply appears as a role with ten permissions.
- The desktop app's existing screens are unchanged.
- An account owner who also held an `admin` membership keeps the same effective access.

**Behaviour change worth stating:** an account owner now has tenant data permissions in the organizations they own. Before, `owner` was account-scoped only and conferred no `transcript:read` or `score:read`; a person needed a separate membership for that. Now owning an organization means being able to read it, which is the point of making it a membership role.

## 11. Testing

| Suite | Result |
| --- | --- |
| `pytest tests/unit` | 110 passed |
| `pytest tests/integration` | 97 passed |
| `./scripts/dev-verify.sh` | 25 passed, 0 failed |
| `alembic upgrade head` | applied against Postgres 16 |
| `base → head → base → head` | clean on a scratch database |
| `uvx pyright@1.1.414` | 0 errors |

New files: `tests/integration/test_signup.py` (7 tests), `tests/integration/test_organizations.py` (6 tests).

Updated: `test_account_api.py` (owner fixtures become owner memberships), `test_opa_bundle.py`, `test_authz.py`, `test_user_provisioning.py`, `test_permissions.py`, `test_public_routes.py`.

### End-to-end against the live stack

`./scripts/dev-up.sh`, then:

1. `POST /signup` → 202, Keycloak identity created, account + organization + owner membership in Postgres, set-password mail in Mailpit
2. Password set, token acquired through `aihr-local-cli`
3. `GET /me` → one membership, role `owner`, ten permissions including `tenant:create`
4. `POST /organizations` → 201, a second account and organization
5. `POST /accounts/{id}/invitations` → 200, colleague joins as recruiter
6. `POST /accounts/{id}/tenants` → 200, the owner-only route
7. `GET /accounts/{other}/members` → 404, cross-account isolation holds

### Two test-hygiene notes

`test_founding_requires_a_token` and `test_a_known_address_is_accepted_without_creating_anything` originally asserted that an organization named "Acme Fintech" did not exist. That is the seed's organization name, so they passed on an empty database and failed against a seeded one. Both now use a per-test UUID name.

## 12. Known gaps and follow-ups

- **No captcha on `/signup`.** It inherits the gateway's per-IP rate limit (120/min), which bounds abuse but does not stop a determined script from creating identities in Keycloak. A captcha needs a frontend and an external service.
- **Compose runs no gateway**, so the `/signup` and `/organizations` routing in `gateway.yaml` is asserted only by `./scripts/k3d-verify.sh`, which was not run on this branch.
- **A Keycloak identity created through the Admin API has no `firstName`/`lastName`.** Under Keycloak 26's default user profile those are required, so a direct-grant token request fails with "Account is not fully set up" until the browser set-password flow collects them. This is pre-existing — the invite path behaves identically — and invisible to anyone using the real flow.
- **No rate limit in Compose.** Local `/signup` is unthrottled.
- **Nothing reclaims an abandoned signup.** An account, organization and owner membership exist from the moment of signup, whether or not the password is ever set.
- **`accounts` is now dormant.** It holds a name, a timestamp and nothing else. If billing never needs a layer above `organizations`, it should be deleted in a later migration rather than left as structure nobody reads.

## 13. File inventory

### New

| File | Lines | What |
| --- | --- | --- |
| `services/api/routes/signup.py` | 65 | `POST /signup` |
| `services/api/routes/organizations.py` | 54 | `POST /organizations`, `found_organization()` |
| `migrations/versions/0005_tenancy_on_memberships.py` | 77 | Backfill and column drop |
| `tests/integration/test_signup.py` | 148 | |
| `tests/integration/test_organizations.py` | 149 | |

### Changed

| File | What |
| --- | --- |
| `packages/core/models.py` | `User.account_id`, `User.account_role` removed; `Account.name` no longer unique |
| `packages/core/permissions.py` | `owner` in `ROLE_PERMISSIONS`; account-role tables removed; `/organizations` in `SELF_ROUTES` |
| `services/api/account_authz.py` | `owns_account()`; membership-derived `is_owner` |
| `services/api/bundle.py` | `account_access` from owner/admin memberships |
| `services/api/routes/account.py` | `account_role` removed; account-scoped membership reads and deletes |
| `services/api/main.py` | Two routers registered |
| `deploy/k8s/gateway.yaml` | `/organizations` and `/signup` routed |
| `apps/desktop/src/main.js` | Shared `send()`; `api:signup` channel |
| `apps/desktop/src/preload.js` | `signup(body)` |
| `apps/desktop/src/renderer/app.js` | Signup form; `foundTenantView()` |
| `apps/desktop/src/renderer/index.html` | Signup fields and toggle |
| `apps/desktop/src/renderer/styles.css` | `.form` grid |
| `scripts/seed.py` | Grants an owner membership instead of setting `account_role` |
| `scripts/grant_membership.py` | `--account`/`--account-role` removed; `--role owner` allowed |
| `scripts/dev-up.sh`, `scripts/k3d-up.sh` | Bootstrap the owner by membership |
| `README.md` | Signup replaces "nothing may create the first owner" |
| `tests/…` (6 files) | Owner fixtures become owner memberships |
