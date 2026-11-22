"use strict";

// ---------------------------------------------------------------- helpers

const $ = (id) => document.getElementById(id);

// Builds DOM without innerHTML, so API data can never inject markup.
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "style") el.style.cssText = value; // CSSOM, permitted by the CSP
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const STORAGE_KEY = "aihr.org";
function remember(value) {
  try { localStorage.setItem(STORAGE_KEY, value); } catch { /* storage unavailable */ }
}
function recall() {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}

const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const fmtDate = (iso) => (iso ? dateFmt.format(new Date(iso)) : "—");
const fmtDateTime = (iso) => (iso ? dateTimeFmt.format(new Date(iso)) : "—");
const pct = (value) => `${Math.round(value * 100)}%`;
const humanize = (s) => String(s || "").replace(/_/g, " ");

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

const STATUS_TONE = {
  completed: "ok",
  inviting: "info", invited: "info", in_progress: "info", ringing: "info", dispatching: "info",
  callback_requested: "warn",
  expired: "bad", lost: "bad", failed: "bad", no_answer: "bad", voicemail: "bad",
  busy: "bad", declined: "bad", unreachable: "bad",
};
const ACTIVE_STATUSES = new Set(["inviting", "invited", "in_progress", "ringing", "dispatching"]);
const STATUS_LABEL = { invited: "invite sent", in_progress: "in call" };

function badge(status) {
  return h("span", { class: `badge ${STATUS_TONE[status] || ""}` }, STATUS_LABEL[status] || humanize(status));
}

function meter(value) {
  const clamped = Math.max(0, Math.min(1, Number(value) || 0));
  return h("div", { class: "meter" },
    h("div", { class: "meter-track" }, h("div", { class: "meter-fill", style: `width:${clamped * 100}%` })),
    h("span", { class: "meter-value" }, pct(clamped)));
}

let toastTimer = null;
function toast(message, tone) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${tone || ""}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4500);
}

// ---------------------------------------------------------------- state and API

const state = { me: null, org: null, pollTimer: null };

function can(permission) {
  return Boolean(state.org && state.org.permissions.includes(permission));
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `Request failed (${status})`);
    this.status = status;
  }
}

async function api(method, path, body) {
  const res = method === "GET" ? await window.aihr.get(path) : await window.aihr.post(path, body);
  if (res.status === 401) {
    showLogin("Your session ended. Please sign in again.");
    throw new ApiError(401, "signed out");
  }
  if (!res.ok) throw new ApiError(res.status, res.data && res.data.detail);
  return res.data;
}

const tenantPath = (suffix) => `/tenants/${state.org.org_id}${suffix}`;

// ---------------------------------------------------------------- auth screens

function showSignup(open) {
  $("signup").hidden = !open;
  $("signup-error").hidden = true;
  $("signup-toggle").hidden = false;
  $("signup-toggle").textContent = open
    ? "Already have an account? Sign in"
    : "New here? Create an organization";
  $("login-btn").hidden = open;
  if (open) $("signup-email").focus();
}

function showLogin(message) {
  stopPolling();
  $("shell").hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !message;
  $("login-error").textContent = message || "";
  showSignup(false);
}

async function submitSignup(event) {
  event.preventDefault();
  const btn = $("signup-btn");
  const error = $("signup-error");
  error.hidden = true;
  btn.disabled = true;
  btn.textContent = "Creating…";

  const res = await window.aihr.signup({
    email: $("signup-email").value.trim(),
    organization_name: $("signup-org").value.trim(),
  });

  btn.disabled = false;
  btn.textContent = "Create organization";
  if (!res.ok) {
    error.hidden = false;
    error.textContent = (res.data && res.data.detail) || `Signup failed (${res.status})`;
    return;
  }

  showSignup(false);
  $("signup-toggle").hidden = true;
  toast("Check your email to set a password, then sign in.", "ok");
}

async function signIn() {
  const btn = $("login-btn");
  btn.disabled = true;
  btn.textContent = "Waiting for sign-in…";
  const result = await window.aihr.login();
  btn.disabled = false;
  btn.textContent = "Sign in with Keycloak";
  if (!result.ok) return showLogin(result.error);
  await enterApp();
}

async function enterApp() {
  try {
    state.me = await api("GET", "/me");
  } catch (err) {
    if (err.status !== 401) showLogin(err.message);
    return;
  }
  $("login").hidden = true;
  $("shell").hidden = false;
  $("whoami").replaceChildren(h("strong", {}, state.me.email || "Signed in"), state.me.is_platform_admin ? "platform admin" : "");

  const select = $("org-select");
  select.replaceChildren(...state.me.memberships.map((m) => h("option", { value: m.org_id }, m.org_name)));
  const saved = state.me.memberships.find((m) => m.org_id === recall());
  state.org = saved || state.me.memberships[0] || null;
  if (state.org) select.value = state.org.org_id;
  select.disabled = state.me.memberships.length < 2;

  if (!location.hash || location.hash === "#") location.hash = "#/applications";
  else route();
}

// ---------------------------------------------------------------- router

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function render(...nodes) {
  $("view").replaceChildren(...nodes.flat(Infinity).filter(Boolean));
}

function loading() {
  render(h("div", { class: "spinner" }, "Loading…"));
}

function failure(err) {
  if (err.status === 401) return;
  const text = {
    403: "You don't have permission to see this.",
    404: "This item doesn't exist, or it belongs to another organization.",
  }[err.status] || err.message;
  render(h("div", { class: "card" }, h("div", { class: "empty" }, text)));
}

async function route() {
  stopPolling();
  if (!state.me) return;
  if (!state.org) return foundTenantView();

  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  try {
    if (parts[0] === "applications" && parts[1] && parts[2] === "calls" && parts[3]) {
      await callView(parts[1], parts[3]);
    } else if (parts[0] === "applications" && parts[1]) {
      await applicationView(parts[1]);
    } else {
      await applicationsView();
    }
  } catch (err) {
    failure(err);
  }
}

// ---------------------------------------------------------------- first organization

function foundTenantView() {
  const input = h("input", { class: "input", maxlength: "200", placeholder: "Acme Fintech" });
  const error = h("p", { class: "error" });
  error.hidden = true;

  const submit = async () => {
    const name = input.value.trim();
    if (!name) return;
    error.hidden = true;
    try {
      const created = await api("POST", "/organizations", { name });
      state.me = await api("GET", "/me");
      state.org = state.me.memberships.find((m) => m.org_id === created.org_id) || null;
      if (state.org) remember(state.org.org_id);
      $("org-select").replaceChildren(
        ...state.me.memberships.map((m) => h("option", { value: m.org_id }, m.org_name)));
      $("org-select").value = created.org_id;
      $("org-select").disabled = state.me.memberships.length < 2;
      toast(`${created.name} is ready.`, "ok");
      route();
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
    }
  };

  render(h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Create your organization")),
    h("div", { class: "card-body" },
      h("p", { class: "muted" },
        "You don't belong to an organization yet. Create one to start screening candidates, " +
        "or ask an admin to invite you to theirs."),
      h("form", { class: "form", onsubmit: (e) => { e.preventDefault(); submit(); } },
        h("div", {},
          h("label", { class: "field-label" }, "Organization name"),
          input),
        h("button", { class: "btn btn-primary", type: "submit" }, "Create organization"),
        error))));
}

// ---------------------------------------------------------------- applications list

const PAGE = 50;

async function applicationsView() {
  loading();
  let rows = await api("GET", tenantPath(`/applications?limit=${PAGE}&offset=0`));
  let exhausted = rows.length < PAGE;
  let filter = "";

  const tbody = h("tbody");
  const count = h("div", { class: "sub" });
  const more = h("button", { class: "btn btn-sm", onclick: loadMore }, "Load more");

  function draw() {
    const needle = filter.trim().toLowerCase();
    const visible = rows.filter((a) =>
      !needle || [a.candidate_name, a.candidate_email, a.job_title, a.stage].some((v) => String(v).toLowerCase().includes(needle)));
    count.textContent = `${rows.length}${exhausted ? "" : "+"} applications${needle ? `, ${visible.length} shown` : ""}`;
    tbody.replaceChildren(...(visible.length ? visible.map((a) =>
      h("tr", { class: "clickable", onclick: () => (location.hash = `#/applications/${a.application_id}`) },
        h("td", {}, a.candidate_name, h("span", { class: "secondary" }, a.candidate_email)),
        h("td", {}, a.job_title),
        h("td", {}, badge(a.stage)),
        h("td", { style: "width:200px" }, a.overall_score === null ? h("span", { class: "muted" }, "—") : meter(a.overall_score)),
        h("td", { class: "num muted" }, fmtDate(a.created_at)))) : [
      h("tr", {}, h("td", { colspan: "5" }, h("div", { class: "empty" }, rows.length ? "No applications match your search." : "No applications yet."))),
    ]));
    more.hidden = exhausted || Boolean(needle);
  }

  async function loadMore() {
    more.disabled = true;
    try {
      const next = await api("GET", tenantPath(`/applications?limit=${PAGE}&offset=${rows.length}`));
      rows = rows.concat(next);
      exhausted = next.length < PAGE;
      draw();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      more.disabled = false;
    }
  }

  const search = h("input", { class: "input", type: "search", placeholder: "Search name, email, role, stage", oninput: (e) => { filter = e.target.value; draw(); } });

  render(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Applications"), count),
      h("div", { class: "toolbar" }, search, h("button", { class: "btn", onclick: route }, "Refresh"))),
    h("div", { class: "card" },
      h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Candidate", "Role", "Stage", "Score", "Applied"].map((t) => h("th", {}, t)))),
        tbody)),
    h("div", { style: "margin-top:12px;text-align:center" }, more));
  draw();
  search.focus();
}

// ---------------------------------------------------------------- application detail

async function applicationView(applicationId) {
  loading();
  const app = await api("GET", tenantPath(`/applications/${applicationId}`));
  const active = app.calls.some((c) => ACTIVE_STATUSES.has(c.status));

  const inviteBtn = h("button", { class: "btn btn-primary", onclick: () => sendInvite(app, inviteBtn), disabled: active }, "Send email invite");
  const actions = can("call:create")
    ? h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Screening")),
        h("div", { class: "card-body" },
          h("div", { class: "actions" },
            inviteBtn,
            h("button", { class: "btn", disabled: true, title: "Phone screening (Twilio/SIP) is not available yet" }, "Phone call · coming soon")),
          h("p", { class: "hint" }, active
            ? "A screening is in progress for this candidate. This page refreshes on its own."
            : `Emails ${app.candidate_email} a personal link to take the AI screening call in their browser. The link works for 72 hours and is re-sent if unused.`)))
    : null;

  const latest = app.scores[0];
  const breakdown = latest ? Object.entries(latest.breakdown).filter(([k, v]) => k !== "overall" && typeof v === "number") : [];

  render(
    h("a", { class: "crumb", href: "#/applications" }, "← Applications"),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, app.candidate_name), h("div", { class: "sub" }, `${app.job_title} · ${app.candidate_email}`)),
      h("div", { class: "toolbar" }, badge(app.stage), h("button", { class: "btn", onclick: route }, "Refresh"))),
    h("div", { class: "grid" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Score"), latest ? h("span", { class: "muted" }, humanize(latest.stage)) : null),
        h("div", { class: "card-body" }, latest
          ? [
              h("div", { style: "font-size:28px;font-weight:650", class: "num" }, app.overall_score === null ? "—" : pct(app.overall_score)),
              breakdown.length ? h("div", { style: "margin-top:12px" }, breakdown.map(([k, v]) =>
                h("div", { class: "rubric-item" }, h("div", { class: "label" }, h("span", {}, humanize(k))), meter(v)))) : null,
            ]
          : h("span", { class: "muted" }, "Not scored yet."))),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Details")),
        h("div", { class: "card-body" },
          h("dl", { class: "kv" },
            h("dt", {}, "Role"), h("dd", {}, app.job_title),
            h("dt", {}, "Email"), h("dd", {}, app.candidate_email),
            h("dt", {}, "Stage"), h("dd", {}, humanize(app.stage)),
            h("dt", {}, "Applied"), h("dd", {}, fmtDateTime(app.created_at)),
            h("dt", {}, "Scores"), h("dd", {}, String(app.scores.length)))))),
    actions,
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Calls")),
      app.calls.length
        ? h("table", { class: "table" },
            h("thead", {}, h("tr", {}, ["Attempt", "Channel", "Status", "Duration", "Created"].map((t) => h("th", {}, t)))),
            h("tbody", {}, app.calls.map((c) =>
              h("tr", { class: "clickable", onclick: () => (location.hash = `#/applications/${app.application_id}/calls/${c.call_id}`) },
                h("td", { class: "num" }, `#${c.attempt_no}`),
                h("td", {}, c.call_mode === "email" ? "email invite" : c.call_mode),
                h("td", {}, badge(c.status)),
                h("td", { class: "num" }, fmtDuration(c.duration_seconds)),
                h("td", { class: "num muted" }, fmtDateTime(c.created_at))))))
        : h("div", { class: "empty" }, "No calls yet.")));

  if (active) {
    state.pollTimer = setTimeout(() => {
      if (location.hash === `#/applications/${applicationId}`) route();
    }, 8000);
  }
}

async function sendInvite(app, button) {
  if (!confirm(`Email a screening invite to ${app.candidate_name} (${app.candidate_email})?`)) return;
  button.disabled = true;
  try {
    await api("POST", tenantPath(`/applications/${app.application_id}/screenings`), { channel: "email" });
    toast("Invite queued. The candidate will get an email shortly.");
    setTimeout(route, 2500);
  } catch (err) {
    button.disabled = false;
    if (err.status === 401) return;
    const text = {
      409: "A screening is already in progress for this candidate.",
      503: "Email invites aren't configured on the server (SMTP and INVITE_SIGNING_KEY).",
    }[err.status] || err.message;
    toast(text, "bad");
  }
}

// ---------------------------------------------------------------- call detail

async function optional(promise) {
  try {
    return await promise;
  } catch (err) {
    if (err.status === 404 || err.status === 403) return { missing: err.status };
    throw err;
  }
}

async function callView(applicationId, callId) {
  loading();
  const [call, summary, transcript] = await Promise.all([
    api("GET", tenantPath(`/calls/${callId}`)),
    optional(api("GET", tenantPath(`/calls/${callId}/summary`))),
    can("transcript:read") ? optional(api("GET", tenantPath(`/calls/${callId}/transcript`))) : Promise.resolve({ missing: 403 }),
  ]);

  render(
    h("a", { class: "crumb", href: `#/applications/${applicationId}` }, "← Application"),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, `Call #${call.attempt_no}`),
        h("div", { class: "sub" }, `${call.call_mode === "email" ? "Email invite" : call.call_mode} · ${fmtDateTime(call.created_at)} · ${fmtDuration(call.duration_seconds)}`)),
      h("div", { class: "toolbar" }, badge(call.status))),
    summaryCard(summary),
    transcriptCard(transcript));
}

function summaryCard(summary) {
  if (summary.missing) {
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "AI summary")),
      h("div", { class: "empty" }, summary.missing === 403 ? "You don't have permission to see summaries." : "No summary yet. Summaries appear after a completed call."));
  }

  const facts = Object.entries(summary.facts || {});
  const listOrNone = (items, none) => (items && items.length ? h("ul", { class: "list" }, items.map((i) => h("li", {}, i))) : h("span", { class: "muted" }, none));

  return [
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "AI summary"), h("span", { class: "badge" }, `${summary.confidence} confidence`)),
      h("div", { class: "card-body" },
        h("p", { class: "summary-text" }, summary.summary),
        h("p", { class: "fine" }, `Model ${summary.model_version} · prompt ${summary.prompt_version}`))),
    h("div", { class: "grid" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Rubric")),
        h("div", { class: "card-body" }, summary.rubric_scores.length
          ? summary.rubric_scores.map((r) =>
              h("div", { class: "rubric-item" },
                h("div", { class: "label" }, h("span", {}, humanize(r.dimension))),
                meter(r.score),
                h("div", { class: "evidence" }, r.evidence)))
          : h("span", { class: "muted" }, "No rubric scores."))),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Facts")),
        h("div", { class: "card-body" }, facts.length
          ? h("dl", { class: "kv" }, facts.flatMap(([k, v]) => [h("dt", {}, humanize(k)), h("dd", {}, typeof v === "boolean" ? (v ? "yes" : "no") : String(v))]))
          : h("span", { class: "muted" }, "No facts extracted.")))),
    h("div", { class: "grid" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Red flags")),
        h("div", { class: "card-body" }, listOrNone(summary.red_flags, "None noted."))),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Candidate's questions")),
        h("div", { class: "card-body" }, listOrNone(summary.candidate_questions, "None asked.")))),
  ];
}

function transcriptCard(transcript) {
  const body = transcript.missing
    ? h("div", { class: "empty" }, transcript.missing === 403 ? "Your role can't read transcripts." : "No transcript for this call.")
    : transcript.turns.length
      ? h("div", { class: "card-body transcript" }, transcript.turns.map((t) =>
          h("div", { class: `turn ${t.speaker === "agent" ? "agent" : "candidate"}` },
            h("div", { class: "who" }, t.speaker === "agent" ? "AI assistant" : "Candidate"),
            t.text)))
      : h("div", { class: "empty" }, "The transcript is empty.");
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Transcript")), body);
}

// ---------------------------------------------------------------- boot

$("login-btn").addEventListener("click", signIn);
$("signup-toggle").addEventListener("click", () => showSignup($("signup").hidden));
$("signup").addEventListener("submit", submitSignup);
$("logout-btn").addEventListener("click", async () => {
  await window.aihr.logout();
  state.me = null;
  state.org = null;
  showLogin();
});
$("org-select").addEventListener("change", (e) => {
  state.org = state.me.memberships.find((m) => m.org_id === e.target.value) || null;
  if (state.org) remember(state.org.org_id);
  if (location.hash === "#/applications") route();
  else location.hash = "#/applications";
});
window.addEventListener("hashchange", route);

(async () => {
  const status = await window.aihr.status();
  $("login-env").textContent = `API ${status.apiUrl} · Keycloak ${status.keycloakUrl}`;
  if (status.signedIn) await enterApp();
  else showLogin();
})();
