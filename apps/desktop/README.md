# aihr desktop

Recruiter app built with Electron. Use it to browse applications, open a candidate's screening calls, AI summaries and transcripts, and email a candidate a screening invite.

It is plain HTML and JavaScript with no build step.

## Run it against the local stack

```
cp .env.example .env              # once
make dev                          # stack up, migrated, seeded, dev users granted
make desktop                      # npm install + launch
```

Sign in as `recruiter` / `recruiter` (or `viewer` / `viewer`). Invite emails land in Mailpit at http://localhost:8025. The link in each email opens the candidate join page at http://localhost:8000/join/….

The app signs in at `http://localhost:8080`. Keycloak stamps that hostname into every token and the API expects it, so nothing outside Docker needs to resolve the `keycloak` service name — see the root README.

The `aihr-desktop` client is in `deploy/dev/realm-aihr-local.json`. Keycloak imports the realm only when its volume is new, so on an existing stack, recreate the volume once: `docker compose rm -sf keycloak keycloak-db && docker volume rm aihr_keycloakdata && make dev`. `make dev` checks for this and tells you.

The voice call itself also needs the provider keys in `.env` and `docker compose --profile agent up -d agent-worker`.

`make desktop` unsets `ELECTRON_RUN_AS_NODE` for you — some editor terminals set it, and Electron then runs as plain Node.

### Configuration

The app reads these environment variables. The defaults match docker compose.

| Variable | Default |
| --- | --- |
| `AIHR_API_URL` | `http://localhost:8000` |
| `AIHR_KEYCLOAK_URL` | `http://localhost:8080` |
| `AIHR_KEYCLOAK_REALM` | `aihr` |
| `AIHR_KEYCLOAK_CLIENT_ID` | `aihr-desktop` |

## How it is put together

| File | Role |
| --- | --- |
| `src/main.js` | Main process. Opens the window and handles every API request, so tokens never reach the page. |
| `src/auth.js` | Keycloak sign-in: the authorization code flow with PKCE, in a separate window. Keycloak redirects to `http://127.0.0.1:47823/callback`; the app intercepts that redirect, so nothing listens on the port. Also refreshes and signs out. |
| `src/preload.js` | The page's only bridge to the main process: `status`, `login`, `logout`, `get`, `post`. |
| `src/renderer/` | The UI: the applications list, application detail with the invite action, and call detail with summary, rubric, facts and transcript. Views are built with DOM APIs, never `innerHTML`. A strict CSP blocks all network access from the page. |

The renderer runs sandboxed, with `contextIsolation` on and `nodeIntegration` off. The UI hides actions your role lacks, using the permissions `GET /me` returns. The API enforces them regardless.

## Phone screening

The **Phone call** button is disabled. Twilio/SIP dialing is not built yet, so the API returns `501` for `channel: "phone"`. For now, email invites are the way to screen a candidate.
