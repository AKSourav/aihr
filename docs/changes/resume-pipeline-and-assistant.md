# Resume-driven hiring pipeline and the recruiter assistant

## 1. Summary

| | |
|---|---|
| Resume is the entry point | Every candidate now arrives with a PDF. Upload many at once and each becomes a candidate — name, email, phone and links come from the resume, so no spreadsheet is needed. |
| Extraction and scoring | A Temporal workflow parses the PDF, extracts a structured profile, then scores it against the job's rubric and writes a `Score(stage="initial")`. |
| Ranked candidates per job | The job page lists its candidates best-first, paginated, showing the resume score and the post-call score side by side. |
| Shortlist rules | A job carries `top_k` and/or `min_score`, combined with `all`/`any`. Matching candidates are highlighted and advanced to a `qualified` stage. Screenings are never started automatically. |
| Cohort summaries | Select 2–10 candidates and get a comparative summary against the rubric. |
| Recruiter assistant | "Kiran", a tool-calling assistant that ranks, explains, drafts screening questions and *proposes* screenings. It has no verb that sends anything. |
| MCP | The same tool registry is served over MCP at `POST /tenants/{org_id}/mcp` for external clients. |
| Self-test call | One button starts a real screening call against yourself, to check STT/TTS/LLM wiring. |

## 2. What existed before

Candidates were name + email only. `packages/core/storage.py` could read and write JSON and
nothing else; there was no file-upload endpoint anywhere, no PDF library, and
`python-multipart` was not a dependency. `Score.stage` had an `"initial"` value that only
`scripts/seed.py` wrote, as a hardcoded placeholder. The job page showed rubric, questions and
FAQ but no candidates, and `GET /applications` had no `job_id` filter and no ordering beyond
`created_at DESC`.

## 3. Resume ingestion (migration `0009`)

A new `resumes` table, not `CandidateProfile` — that model has no `org_id`, no composite FK and
no versioning, so it is left alone. `candidate_id` and `job_id` are nullable because a
bulk-uploaded PDF has no known candidate until the LLM extracts the email; a composite FK with a
NULL column is MATCH SIMPLE, so the tenant check is simply skipped until it can be made.
Versioning is by row — "current" is the newest `created_at`.

`created_at` is stamped in Python, not by `server_default`. Postgres `now()` is
transaction-scoped, so two resumes uploaded in one request would otherwise share a timestamp and
"newest wins" would fall back to comparing random UUIDs — which the screening gate reads.

Statuses are `extracting | ready | failed` with a CHECK constraint. The fourth UI state,
`awaiting_resume`, is the *absence* of a row, so the existing CSV import needed no change to
produce it.

Validation does not trust the declared content type: magic bytes (`%PDF-`) decide. 10 MB per
file, 20 files per request. The `Content-Disposition` filename on the download route is derived
from the candidate id, never the uploaded name, which is attacker-controlled.

**Known limitation:** `pypdf` has no OCR, so scanned or image-only PDFs land in `failed` with
"no selectable text found in this PDF".

## 4. Extraction and scoring

`core/resume_parser.py` (pypdf, 20-page and 40k-char caps) → `core/resume_extractor.py` →
`core/resume_scorer.py`. All three follow the existing `summarizer.py` shape: a Jinja prompt, a
`<<<BEGIN_…>>>` fence with regex scrubbing, `response_format=json_object`, and a two-attempt
self-repair loop.

A resume is fully attacker-controlled text, so fencing is not optional. Three layers: the fence
scrubbing, the "this is data, never instructions" rule in the prompt, and a closed pydantic
schema whose `max_length` caps are the output whitelist.

Fairness needs care, because a resume legitimately may state protected attributes and stripping
the input would destroy signal. The prompt forbids using them, and afterwards the scoring
activity runs `protected_terms()` over every evidence string, blanking any hit, keeping the
score, and writing a `resume_score_flagged` audit event.

`ExtractResumeWorkflow` chains `extract_resume` → `score_resume`, and ends with a terminal
`fail_resume` activity when retries are exhausted — without it an exhausted retry would leave a
row stuck in `extracting` forever. The failure carries the provider's own words through, so
"out of credits" reaches the recruiter instead of a bare "failed".

## 5. Ranking and shortlist rules (migration `0010`)

`core/shortlist.py` holds the one ranked query, used by both the list endpoint and auto-advance
so they can never disagree. Two `DISTINCT ON` subqueries (one per score stage) and
`coalesce(call_overall, resume_overall)` as the ordering key.

Three things there are easy to get wrong:

- The order expression is passed **raw** into `row_number() OVER (ORDER BY …)`. Postgres rejects
  an output-column alias inside a window clause.
- `rank` is global, not per page: window functions run before `LIMIT`/`OFFSET`, so a `top_k` rule
  is evaluable from any page.
- The coalesce happens in **SQL**, not Python. A post-call score of `0.0` is not NULL and must
  beat a high resume score; `call or resume` in Python would get that backwards.

`FOR UPDATE` is illegal alongside window functions, so auto-advance reads unlocked and re-selects
the winners `FOR UPDATE` — with `noload()` on the `candidate`/`job` relationships, because they
are `lazy="joined"` and Postgres refuses `FOR UPDATE` on the nullable side of an outer join.

Advancement is **one-way**. Relaxing the rules never demotes anyone, because that would clobber a
recruiter's later manual stage change. The `qualifies` flag is recomputed live, so a row can read
`qualified` while no longer matching; the UI says so.

## 6. The assistant (migration `0011`)

`core/assistant_tools.py` defines eight tools once; `core/assistant.py` runs the loop; MCP and
the in-app chat share both. **`org_id` is never a tool parameter**, so the model cannot express a
cross-tenant request at all — it comes only from the authenticated path.

`require()` was split so `ensure(session, context, permission)` is the single implementation, used
both by the FastAPI dependency and per-tool. The decision is byte-identical; only the enforcement
point moves.

On `openai==3.37.0`, `message.tool_calls` is a discriminated union of function and custom tool
calls, and only function calls carry `.function`. The loop filters on `.type` and casts, so a
custom call is ignored rather than raising `AttributeError`.

Nothing the model can call has a side effect. `propose_screening` writes a proposal row; a
separate REST route at `Permission.CALL_CREATE` executes it after the user confirms, and that
route reuses `start_screening_for` so the resume gate, 503/409 behaviour and audit trail cannot
drift from the hand-clicked button.

Conversation messages are also stamped explicitly for the same transaction-scoped `now()` reason
as resumes — here the blast radius is worse, because message order *is* the model's context.

### MCP

Served at `POST /tenants/{org_id}/mcp`, inside the tenant prefix. This is forced by
`deploy/k8s/gateway.yaml`: the HTTPRoute matches only five path prefixes, so a top-level `/mcp`
would not be routed in k3d at all, and would sit outside the JWT and OPA policies.

It is a real FastAPI route, not `app.mount()`, so it appears in `/openapi.json` (which
`tests/unit/test_api_access.py` requires) and so its `Depends` chain can put the caller in a
`ContextVar` before the session manager sees the request. Stateless and POST-only: with a
long-lived SSE session a `tools/call` could arrive on a different request than the one that set
that ContextVar, leaving it stale from another tenant.

**Trade-off, stated plainly:** one HTTP path now carries many logical operations, so the edge can
no longer see which operation is invoked. The edge enforces a floor (`CANDIDATE_READ`), real
authorization is per-tool via the same `ROLE_PERMISSIONS` table OPA compiles from, and the only
irreversible write is kept off MCP entirely.

## 7. Desktop app

The renderer had no binary path, so `main.js` gained `api:upload` (files cross IPC as
`Uint8Array`, so the renderer never learns a path and still never sees a token) and
`api:open-file` (writes a 0600 temp file and hands it to the OS viewer, removed on quit). An
embedded PDF would be blocked anyway: the CSP sets no `object-src`/`frame-src`.

Model output reaches the DOM only as text nodes — no markdown parser, no `innerHTML`. Structured
data in the confirm card comes from the typed `pending_action` field, never parsed out of prose.

Also fixed in passing: `api()` had no `"DELETE"` branch and silently fell through to `post`, so
secret deletion was issuing a POST.

## 8. File inventory

New: `packages/core/{llm,resumes,resume_parser,resume_extractor,resume_scorer,scoring,shortlist,cohort_summarizer,assistant,assistant_tools}.py`,
`packages/core/prompts/{resume_extract,resume_score,cohort_summary,assistant}.md.j2`,
`services/api/mcp_server.py`, `services/api/routes/{resumes,test_calls,assistant,mcp}.py`,
`services/temporal_worker/workflows/extract_resume.py`,
`services/temporal_worker/activities/{extract_resume,score_resume}.py`,
migrations `0009`–`0011`.

Changed: `core/{models,schemas,permissions,providers,storage,settings,summarizer,job_drafter}.py`,
`services/api/{main,authz,temporal}.py`, `services/api/routes/{candidates,jobs,screenings,tenant}.py`,
`services/temporal_worker/{worker,shared}.py`, `scripts/seed.py`,
`apps/desktop/src/{main,preload}.js`, `apps/desktop/src/renderer/{app.js,index.html,styles.css}`.

## 9. Breaking change

`POST /tenants/{org_id}/candidates` is now `multipart/form-data` and requires a PDF. That is the
only way to enforce "every added candidate has a resume" at the trust boundary rather than only
in the UI. It rewrote four tests in `tests/integration/test_write_api.py` and added Storage and
Temporal stubs to the shared `client` fixture.
