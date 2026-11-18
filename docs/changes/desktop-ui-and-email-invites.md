# Recruiter desktop app and email screening invites

Branch: `feature/desktop-ui-email-invites`, based on `main` at `f694f4a` ("Full local development on Docker Compose (#13)").
Status: **not committed, not pushed, no PR yet.**

This document covers everything the branch changes, the bugs fixed along the way, how it was tested, and what is still open.

## Contents

1. [Summary](#1-summary)
2. [What existed before](#2-what-existed-before)
3. [Recruiter desktop app](#3-recruiter-desktop-app-electron)
4. [Email invite screening flow](#4-email-invite-screening-flow)
5. [Old invite links resolve to the current invite](#5-old-invite-links-resolve-to-the-current-invite)
6. [API, auth and edge changes](#6-api-auth-and-edge-changes)
7. [Configuration and local development](#7-configuration-and-local-development)
8. [Bug fixes](#8-bug-fixes)
9. [Backward compatibility](#9-backward-compatibility)
10. [Testing](#10-testing)
11. [Known gaps and follow-ups](#11-known-gaps-and-follow-ups)
12. [File inventory](#12-file-inventory)

---

## 1. Summary

| | Before | After |
| --- | --- | --- |
| Recruiter UI | none | Electron desktop app (`apps/desktop/`) |
| Starting a screening | developer scripts only | `POST /tenants/{org_id}/applications/{application_id}/screenings`, from the app |
| Ways to reach a candidate | `web` (agent waits 120 s for a join), `sip` (planned, not implemented) | adds **`email`**: invite link, candidate joins whenever they like within 72 h |
| Email | none | SMTP invites with HTML and text parts; Mailpit in Compose |
| Re-sent invites | n/a | any earlier link for the same application forwards to the current invite |
| "Who am I" for clients | none | `GET /me`: memberships, roles, permissions |

Size: 25 tracked files changed (+343 / −49) and about 3,700 lines in new files, about half of them tests and the desktop app.

## 2. What existed before

The backend on `main` already had:

- **Voice agent** (`services/agent/`, LiveKit Agents). It runs the screening conversation: introduces itself as an AI, asks for recording consent, works through the job's question plan, and handles callbacks and voicemail.
- **`ScreeningCallWorkflow`** (Temporal). It places the call within business hours (IST), waits for the outcome, retries unanswered calls, and marks the candidate unreachable.
- **Post-call summary**: LLM rubric scores, extracted facts, red flags, a fairness filter and moderation.
- **Multi-tenant auth**: Keycloak JWTs, OPA and Envoy at the edge, `org_id` scoping on every query, audited break-glass access.
- A **read-only tenant API** (applications, calls, summaries, transcripts) and internal endpoints for the agent and worker.

Recruiters had no UI, and nothing outside a developer script could start a screening. There was no email, and no way for a candidate to join at a time of their choosing.

## 3. Recruiter desktop app (Electron)

Location: `apps/desktop/`. It is plain HTML, CSS and JavaScript on Electron 33.2.1, with no build step.

### Screens

- **Sign in.** Keycloak authorization code flow with PKCE, in a separate window.
- **Organization picker**, filled from `GET /me`. The choice is remembered per machine.
- **Applications list.** Searchable by name, email, role and stage; loads 50 at a time with "Load more".
- **Application detail.** Score with breakdown, details, a list of calls, and a **Screening** card with **Send email invite**. **Phone call** is disabled and labelled "coming soon". While a screening is in progress the invite button is disabled and the page refreshes every 8 seconds.
- **Call detail.** AI summary with confidence, rubric bars with evidence, facts, red flags, the candidate's questions, and the transcript as a chat.
- Light and dark mode, following the OS setting.

### Security model

| Concern | How it is handled |
| --- | --- |
| Tokens | Held only in the main process (`src/auth.js`) and never sent to the renderer. Refreshed 30 s before expiry; a failed refresh signs the user out. |
| API access | The renderer calls the API through `preload.js` (`get` and `post` by path only). The main process attaches the token and rejects absolute or `//` paths and any method other than GET or POST. |
| Renderer isolation | `sandbox: true`, `contextIsolation: true`, `nodeIntegration: false`. Navigation is blocked and new windows open in the system browser. |
| XSS | Views are built with DOM APIs and `textContent`, never `innerHTML`. The CSP is `default-src 'self'; connect-src 'none'`. |
| Sign-in redirect | Keycloak redirects to the loopback URI `http://127.0.0.1:47823/callback`. The app intercepts the navigation; nothing listens on the port. `state` is checked. |
| Sign-out | Calls Keycloak's logout endpoint and clears the sign-in window's session storage. |
| Roles | The UI hides actions the role lacks, using the permissions from `/me`. The API enforces them regardless. |

### Configuration

| Variable | Default |
| --- | --- |
| `AIHR_API_URL` | `http://localhost:8000` |
| `AIHR_KEYCLOAK_URL` | `http://keycloak:8080` (the issuer pinned by `main`'s Compose setup) |
| `AIHR_KEYCLOAK_REALM` | `aihr` |
| `AIHR_KEYCLOAK_CLIENT_ID` | `aihr-desktop` |

## 4. Email invite screening flow

### Design choice

The existing `web` mode dispatches the agent first, and the candidate then has 120 s to join. That cannot work for an email, which is read hours later. In `email` mode **no room exists until the candidate clicks Join**. Clicking Join creates the room, dispatches the agent and connects the candidate in one step.

### Sequence

```
Recruiter (desktop) ── POST …/applications/{id}/screenings {channel: "email"}
  └─► Temporal ScreeningCallWorkflow (call_mode = "email")
        ├─ activity send_invite: create Call (status "invited"), email a signed link (72 h)
        ├─ wait up to 72 h for "call_joined" or "call_completed"
        │
Candidate clicks link ── GET /join/{token}           (page served by the API)
                     ── GET /public/invites/{token}  (company, role, first name, expiry, state)
                     ── POST /public/invites/{token}/join
                          ├─ lock the call row, create the LiveKit room, dispatch the agent
                          ├─ status → "in_progress"; audit "invite_joined"
                          ├─ signal workflow "call_joined"  → wait switches to the normal call timeout
                          └─ return LiveKit URL + candidate access token
Browser connects (livekit-client) ◄──► voice agent runs the screening (unchanged)
Agent ── POST /internal/calls/{id}/complete ── signal "call_completed" (unchanged)
        ├─ completed           → summarize_call (unchanged)
        ├─ callback_requested  → sleep until callback time, send a new invite
        ├─ declined            → stop
        └─ link unused (72 h)  → expire_invite, send a new invite at once;
                                 after 3 unreachable attempts → mark_unreachable
```

### Workflow (`services/temporal_worker/workflows/screening_call.py`)

- A new `email` branch. The phone/web branch is unchanged step for step (see [§9](#9-backward-compatibility)).
- A new signal, `call_joined(call_id)`. It is accepted only for the current attempt's call id. Once a candidate joins, the wait switches from the invite TTL to `signal_timeout_min`, so a join just before expiry is not cut off.
- Email attempts skip the business-hours wait; the candidate picks the time. An expired invite is re-sent immediately rather than after the 1 h / 4 h back-off, because it has already waited 72 h.
- `ScreeningCallInput` gains `invite_ttl_hours: int = 72`, with a default so existing serialized inputs still load.

### New activities (`services/temporal_worker/activities/send_invite.py`)

- **`send_invite`** creates the `Call` row (`call_mode = "email"`), signs the link, renders and sends the email, sets `invite_sent_at`, `invite_expires_at` and `status = "invited"`, and writes an `invite_sent` audit event. It is idempotent on `invite_sent_at`. Delivery is at least once: a crash after sending but before commit re-sends the email.
- **`expire_invite`** sets `status = "expired"`, but only while the call is still `invited`, and writes an `invite_expired` audit event.

### Invite links (`packages/core/invites.py`)

- An HS256 JWT signed with `INVITE_SIGNING_KEY` (at least 32 characters, enforced), with claims `sub = call_id`, `purpose = "call_invite"` and `exp`.
- The link is `{PUBLIC_BASE_URL}/join/{token}`.

### Email (`packages/core/email.py`, `packages/core/prompts/email/`)

- Plain-text and HTML parts. The email states that the assistant is an AI, the call length, and the link's expiry in IST.
- Re-sent invites (attempt 2 or later) get the subject `Reminder: …` and the line *"Any earlier link we sent you will also take you to this call."*
- Sent over SMTP with optional STARTTLS and login. Compose uses Mailpit.

### Candidate join page (`services/api/static/join.html`)

- Served by the API at `/join/{token}`. It uses `livekit-client@2.23.0`, the version the repo already used.
- States: loading, invalid, expired, finished, lobby, in call (speaking indicator, mute, leave) and ended. Includes a rejoin path and a note when an older link was forwarded.
- Light and dark mode; works on phones.

### Voice agent (`services/agent/`)

- In `email` mode the agent waits for the participant, as `web` mode does.
- In `email` mode only, the prompt gains one sentence: the candidate joined from an invite and has already chosen this time. For every other mode the rendered prompt is byte-for-byte unchanged; this was verified.

### Data model

Migration `0003_call_invites` adds two nullable columns to `calls`: `invite_sent_at` and `invite_expires_at`. Upgrade, downgrade and re-upgrade were all tested.

Call status values used by email mode: `inviting` → `invited` → `in_progress` → the existing terminal statuses, or `expired`.

## 5. Old invite links resolve to the current invite

**Problem.** A candidate misses the first email. The invite expires and a reminder (a new call with a new link) goes out. The candidate then clicks the **first** link. Before this change they saw *"This link has expired"*, or *"This call has already finished"* if the first call ended in a callback request, even though a valid invite was in their inbox.

**Behaviour now.** Any genuine link for an application resolves to that application's **current** invite:

1. Verify the signature. A forged link returns 404. A genuine link is readable for up to **30 days** after expiry (`FORWARD_GRACE`), and only for forwarding.
2. If the link's own call is still usable (invited and unexpired, or in progress) and the link itself is unexpired, use that call.
3. Otherwise find the newest live email invite for the **same application and org**: in progress, or invited and unexpired.
4. If none exists, keep the old answer: 410 for an expired link, "finished" or 409 for a finished call.

Guarantees:

- The agent is only ever dispatched into the current call. The old call is never revived; its row is left as it was.
- The workflow receives `call_joined` with the **current** call id, which is the only id it accepts.
- If the current call is already running, for example after a dropped connection, the candidate rejoins that room and no second agent is dispatched.
- A link can never reach another application, candidate or tenant.
- The join is audited: `invite_joined` with `{"via_link_for_call_id": "<old call id>"}`.
- `GET /public/invites/{token}` returns `forwarded: true`, and the join page shows *"You opened a link from an earlier email. That's fine: it takes you to your current invite."*

No workflow change was needed. The resolution lives in `services/api/routes/public_invites.py` and `core.invites.read_invite_token`.

## 6. API, auth and edge changes

### New endpoints

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /me` | any verified JWT | the caller's memberships, roles and permissions |
| `POST /tenants/{org_id}/applications/{application_id}/screenings` | `call:create` | start a screening. `channel: "email"` returns 202; `"phone"` returns 501; already running returns 409; email not configured returns 503; another tenant's application returns 404 |
| `GET /join/{token}` | invite token | candidate join page |
| `GET /public/invites/{token}` | invite token | invite details and state (`ready`, `in_progress`, `finished`), plus `forwarded` |
| `POST /public/invites/{token}/join` | invite token | create the room and dispatch the agent on the first join; return LiveKit credentials |

Starting a screening writes a `screening_started` audit event. It uses the workflow id `call-{job_id}-{candidate_id}`, the same id the existing completion signal uses.

### Permissions and OPA

- `packages/core/permissions.py`: the screenings route is mapped to `call:create`. A new `SELF_ROUTES` table (`GET /me`) is exported in the OPA bundle as `self_routes`.
- `deploy/opa/policy.rego`: a new rule allows any non-empty subject on `self_routes`. A bundle without `self_routes` behaves exactly as before; there is a test for this.

### Gateway (`deploy/k8s/gateway.yaml`)

- `/me` is added to the authenticated `aihr-api` HTTPRoute.
- A new `aihr-public` HTTPRoute (`/join/`, `/public/invites/`) sits outside the `aihr-authn` SecurityPolicy, because candidates have no Keycloak account. The rate-limit policy also targets it. The local overlay strips its hostname, as it does for `aihr-api`.

### Keycloak

An `aihr-desktop` public client (PKCE S256, loopback redirect, `aihr-api` audience mapper) is added to `deploy/keycloak/realm-aihr.json` and `deploy/dev/realm-aihr-local.json`. Only lines were added; existing clients are untouched.

### Refactors

- `core/livekit_rooms.py`: `create_room_and_dispatch` and `candidate_join_token`. `place_call` uses the helper and makes the same LiveKit calls as before.
- `services/api/temporal.py`: `temporal_client()` (a fresh connection per call, as before) and `screening_workflow_id()`, used by `internal_calls.py`.

## 7. Configuration and local development

### New settings (all optional, with defaults)

| Setting | Default | Notes |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | base of the links in emails |
| `INVITE_SIGNING_KEY` | empty | required for email invites; at least 32 characters (`openssl rand -hex 32`) |
| `INVITE_TTL_HOURS` | `72` | |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_FROM` | empty, `1025`, empty, empty, `false`, `aihr <no-reply@aihr.local>` | `.env.example` points them at Mailpit |
| `MAILPIT_PORT` | `8025` | host port of the Mailpit inbox |

### Compose and Make

- A new `mailpit` service (`axllent/mailpit:v1.21`) serves its web inbox on `${MAILPIT_PORT:-8025}`. The API and worker send to `mailpit:1025` inside the network.
- `make desktop` runs `npm install` and `npm start` in `apps/desktop`.
- `.dockerignore` excludes `apps/`, so the desktop `node_modules` stays out of image builds. `apps/desktop/.gitignore` ignores `node_modules/` and `out/`.

### Running it

```
cp .env.example .env                      # set INVITE_SIGNING_KEY
echo '127.0.0.1 keycloak' | sudo tee -a /etc/hosts   # once; main pins the issuer to keycloak:8080
make dev                                  # main's script: up, migrate, seed, grant dev users
make desktop                              # sign in as recruiter / recruiter
```

Invites land in Mailpit at http://localhost:8025. A real voice call also needs the LiveKit, Deepgram, Cartesia and LLM keys in `.env`, plus `docker compose --profile agent up -d agent-worker`. On an existing Keycloak volume the new client is not imported; recreate the volume as `main`'s README describes.

## 8. Bug fixes

Bugs found and fixed while building and testing this branch. Each was caught by a test, a screenshot review or a design review, and none reached `main`.

| # | Bug | Impact | Fix | Found by |
| --- | --- | --- | --- | --- |
| 1 | **HTML in invite emails was not escaped.** `select_autoescape(["html"])` never matched `invite.html.j2`, whose extension is `.j2`. | Candidate or company names could inject HTML into the email (XSS). | Autoescape keyed on `html.j2`. Covered by `test_html_part_escapes_candidate_supplied_values`. | unit test |
| 2 | **Old invite links dead-ended.** After a re-sent invite, the first link showed "expired", or "already finished" after a callback request. | A candidate holding a valid invite could not join, or gave up. | Link resolution to the current invite ([§5](#5-old-invite-links-resolve-to-the-current-invite)). 8 integration tests. | scenario review |
| 3 | **A late join could be cut off by the invite TTL.** A candidate joining just before expiry would have the workflow time out mid-call, mark the call lost and send a new invite. | Valid calls lost. | `call_joined` signal; the wait switches to the call timeout after a join. Covered by `test_a_join_extends_the_wait_past_the_invite_ttl`. | design review |
| 4 | **The 120 s join window could not work for email.** Web mode dispatches the agent before the candidate is present. | The agent would time out long before the email was read. | The agent is dispatched only when the candidate clicks Join. | design review |
| 5 | **Desktop: score meters were invisible.** The CSP (`style-src 'self'`) blocked inline `style` attributes. | Rubric and score bars rendered empty. | Styles set through the DOM style API, which the CSP permits. | code review before the first run |
| 6 | **Desktop: call detail would not render.** `render()` passed nested arrays to `replaceChildren`. | The summary section showed nothing, or text like `[object HTMLDivElement]`. | `render()` flattens and filters its children. | code review before the first run |
| 7 | **Desktop: switching organization loaded the page twice.** The hash change and a direct `route()` call both fired. | Duplicate API calls and flicker. | Route directly only when the hash is unchanged. | code review |
| 8 | **Desktop: cards in grids were misaligned.** `.card + .card` margins applied inside grids, and stacked grids touched. | Details, Facts and Questions cards sat lower than their neighbours. | `.grid > .card { margin-top: 0 }` and `.grid + .grid` spacing. | screenshot review |
| 9 | **Join page: expiry showed seconds** (`20/11/2026, 01:09:39`). | Cosmetic. | Medium date with short time. | screenshot review |
| 10 | **The API image could not import the worker's dataclass.** `screenings.py` imported `ScreeningCallInput`, but the API Dockerfile copies only `services/api/`. | The API would fail to start in Docker. | The workflow input is passed as a plain dict, which serializes to the same JSON. | Dockerfile review |
| 11 | **Desktop sign-in would fail on `main`'s Compose setup.** After the rebase, `main` pins the issuer to `http://keycloak:8080`; this branch's earlier `KEYCLOAK_ISSUER_URL=http://localhost:8080` would have made the API reject `make dev-token` tokens. | Broken sign-in for the whole team's dev flow. | Issuer override removed; the desktop app uses `http://keycloak:8080`, as `main` documents. | rebase review |
| 12 | **The branch tracked `origin/main` as its upstream** after the rebase. | A plain `git push` could have targeted `main`. | Upstream unset. | rebase review |
| 13 | **The desktop `node_modules` was neither git-ignored nor docker-ignored.** | About 200 MB would be committed and sent with every image build. | `apps/desktop/.gitignore`, and `apps/` in `.dockerignore`. | `git status` review |

Self-introduced regressions caught before they landed are covered in [§9](#9-backward-compatibility).

## 9. Backward compatibility

The requirement was not to break existing behaviour. Every change is additive, and these points were checked:

- **Workflow replay safety.** The phone/web branch of `ScreeningCallWorkflow` issues the same commands in the same order as before: business-hours wait, `uuid4`, `place_call`, then resetting the outcome. Two early edits that moved this ordering were reverted. Screenings already running replay cleanly.
- **Temporal client.** An early version cached one client across requests; it was reverted to the original connect-per-call behaviour.
- **Agent prompt.** Rendered byte for byte against the original template for non-email calls: identical.
- **First-invite email.** The text for attempt 1 is unchanged, and a test pins it.
- **Token API.** `verify_invite_token` behaves exactly as before. Forwarding uses the new `read_invite_token`.
- **Schema.** Only nullable columns were added, and the downgrade works.
- **Settings.** Every new setting has a default. Email only fails, with a 503, when someone actually starts an email screening without configuration.
- **OPA and gateway.** Only additions. The policy works with old bundles that lack `self_routes`.
- **Realm files.** Only lines added; no existing JSON was reformatted.
- **Existing tests.** All pre-existing unit, integration and policy tests pass unchanged. The one test edited was one of this branch's own (`test_an_expired_invite_is_gone`), split across applications because forwarding legitimately links invites on the same application.

## 10. Testing

### Automated, on the rebased branch

| Suite | Result | New in this branch |
| --- | --- | --- |
| Unit (`tests/unit`, excluding workflow tests) | **91 passed** | invite tokens (13), email (5), public routes (6) |
| Workflow (`tests/unit/test_screening_workflow.py`) | **5 passed** before the rebase; see the note below | email completed, expiry and re-invite, late join, stale signals ignored, phone path unchanged |
| Integration (`tests/integration`, Postgres) | **60 passed** | invite API (21): join, rejoin, expiry, forwarding (8), screenings 202/409/501/503/403/404, `/me` |
| OPA policy (`opa test`) | **15 passed** | 4 (`/me` allow, subject required, method, old bundle) |
| Type check (`pyright@1.1.414`) | **0 errors** | |
| Desktop syntax (`npm run check`) | pass | |
| Kustomize (`deploy`, `deploy/local`) | both build | |

**Note on the workflow tests.** They use Temporal's time-skipping test server, which `temporalio` downloads on each run. On the rebased branch that download stalled twice (network), so the 5 tests did not complete there. The workflow file and its test file are unchanged since they passed (5/5) before the rebase. Re-run them before merging, or in CI.

### End to end (throwaway stack, before the rebase)

Postgres, Temporal, Keycloak, Mailpit, the API and the worker were run from source:

- PKCE sign-in with the `aihr-desktop` client against Keycloak's real login form; issuer and audience accepted by the API.
- `/me` and the applications list. Viewer starting a screening → 403; `phone` → 501; first `email` → 202; second → 409.
- The invite email arrived in Mailpit with both parts. The join page loaded (200) and the invite info returned `ready`.
- Join → 502, as expected without LiveKit credentials, and the call stayed `invited` so a retry is possible.
- The agent's `/internal/calls/{id}/context` served the email call. Audit rows were written.
- Desktop screenshots of every view in light and dark mode, captured through the real renderer against the live API.

### Live demo (your machine, before the rebase)

Compose stack, seeded data, desktop app launched. Sign-in, invite and Mailpit worked; Join stopped at missing LiveKit credentials, as expected.

## 11. Known gaps and follow-ups

| Item | Status |
| --- | --- |
| Live voice call through email mode | Not exercised: needs LiveKit, Deepgram, Cartesia and LLM keys. The agent conversation code is the existing web-mode path. |
| Workflow tests on the rebased branch | Re-run in CI or once the Temporal test-server download works (see [§10](#10-testing)). |
| Desktop sign-in on the rebased setup | Needs `127.0.0.1 keycloak` in `/etc/hosts` and a fresh Keycloak volume; not yet clicked through on the rebased stack. |
| Phone screenings (Twilio/SIP) | Not built. The API returns 501 and the UI button is disabled. |
| Creating jobs and candidates, inviting users | No endpoints yet; data comes from the seed. |
| Email delivery guarantee | At least once (see `send_invite`). |
| Desktop packaging | Run with `npm start`. No signed `.dmg` or `.exe`, and no app icon yet. |
| `feature/10-account-admin-groundwork` | A teammate's in-progress branch (32 files). Pull and re-test after it merges; it may touch the same API files. |
| `backend-workflows` (resume scoring) | Local only and built on pre-rewrite history; not in this branch. |
| `.vscode/` | Your editor folder; leave it out of the commit. |

## 12. File inventory

### New

```
apps/desktop/                                   Electron app (package.json, README, src/{main,auth,preload}.js, src/renderer/*)
docs/changes/desktop-ui-and-email-invites.md    this document
migrations/versions/0003_call_invites.py        invite_sent_at, invite_expires_at
packages/core/email.py                          invite rendering and SMTP sending
packages/core/invites.py                        signed links, read/verify, forwarding grace
packages/core/livekit_rooms.py                  room creation, agent dispatch, candidate token
packages/core/prompts/email/invite.{txt,html}.j2
services/api/routes/me.py                       GET /me
services/api/routes/screenings.py               POST …/screenings
services/api/routes/public_invites.py           join page and public invite endpoints, link resolution
services/api/static/join.html                   candidate join page
services/api/temporal.py                        Temporal client and workflow id helpers
services/temporal_worker/activities/send_invite.py   send_invite, expire_invite
tests/unit/test_invites.py, test_invite_email.py, test_public_routes.py, test_screening_workflow.py
tests/integration/test_invites_api.py
```

### Modified

```
.dockerignore, .env.example, Makefile, docker-compose.yml
deploy/README.md, deploy/k8s/gateway.yaml, deploy/local/kustomization.yaml
deploy/keycloak/realm-aihr.json, deploy/dev/realm-aihr-local.json
deploy/opa/policy.rego, deploy/opa/policy_test.rego
packages/core/{models,permissions,schemas,settings}.py
services/agent/main.py, services/agent/prompts/{__init__.py,system.md.j2}
services/api/{bundle,main}.py, services/api/routes/internal_calls.py
services/temporal_worker/{shared,worker}.py
services/temporal_worker/activities/place_call.py
services/temporal_worker/workflows/screening_call.py
```
