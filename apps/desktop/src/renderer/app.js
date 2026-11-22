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

// The API returns an account id only when an organization is created, so the desktop app
// keeps the org -> account mapping itself. Never holds anything secret.
const ACCOUNT_KEY = "aihr.accounts";
function accountMap() {
  try { return JSON.parse(localStorage.getItem(ACCOUNT_KEY) || "{}") || {}; } catch { return {}; }
}
function accountFor(orgId) {
  const value = accountMap()[orgId];
  return typeof value === "string" ? value : null;
}
function rememberAccount(orgId, accountId) {
  try {
    localStorage.setItem(ACCOUNT_KEY, JSON.stringify({ ...accountMap(), [orgId]: accountId }));
  } catch { /* storage unavailable */ }
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
  created: "ok", matched: "info", duplicate: "warn", invalid: "bad",
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
    // FastAPI validation errors arrive as an array of {loc, msg} objects.
    const text = Array.isArray(detail)
      ? detail.map((e) => `${((e && e.loc) || []).slice(-2).join(".")}: ${e && e.msg}`).join("; ")
      : detail;
    super(text || `Request failed (${status})`);
    this.status = status;
  }
}

async function api(method, path, body) {
  const res = method === "GET"
    ? await window.aihr.get(path)
    : method === "PUT"
      ? await window.aihr.put(path, body)
      : await window.aihr.post(path, body);
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

function setActiveNav(name) {
  for (const el of document.querySelectorAll("[data-nav]")) {
    el.classList.toggle("active", el.getAttribute("data-nav") === name);
  }
}

async function route() {
  stopPolling();
  if (!state.me) return;
  if (!state.org) return foundTenantView();

  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  setActiveNav(parts[0] === "jobs" || parts[0] === "providers" ? parts[0] : "applications");
  try {
    if (parts[0] === "providers") {
      providersView();
    } else if (parts[0] === "jobs" && parts[1] === "new") {
      newJobView();
    } else if (parts[0] === "jobs" && parts[1]) {
      await jobView(parts[1]);
    } else if (parts[0] === "jobs") {
      await jobsView();
    } else if (parts[0] === "applications" && parts[1] && parts[2] === "calls" && parts[3]) {
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
      rememberAccount(created.org_id, created.account_id);
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

// ---------------------------------------------------------------- jobs list

async function jobsView() {
  loading();
  const rows = await api("GET", tenantPath(`/jobs?limit=${PAGE}&offset=0`));

  render(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Jobs"), h("div", { class: "sub" }, `${rows.length}${rows.length < PAGE ? "" : "+"} jobs`)),
      h("div", { class: "toolbar" },
        can("job:write") ? h("button", { class: "btn btn-primary", onclick: () => (location.hash = "#/jobs/new") }, "New job") : null,
        h("button", { class: "btn", onclick: route }, "Refresh"))),
    h("div", { class: "card" },
      h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Role", "Created"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows.length
          ? rows.map((j) =>
              h("tr", { class: "clickable", onclick: () => (location.hash = `#/jobs/${j.job_id}`) },
                h("td", {}, j.title),
                h("td", { class: "num muted" }, fmtDate(j.created_at))))
          : h("tr", {}, h("td", { colspan: "2" }, h("div", { class: "empty" },
              h("p", { style: "margin:0 0 12px" }, "No jobs yet. A job carries the rubric and the questions the AI screens against."),
              can("job:write") ? h("button", { class: "btn btn-primary", onclick: () => (location.hash = "#/jobs/new") }, "Create your first job") : null)))))));
}

// ---------------------------------------------------------------- create job

function newJobView() {
  const title = h("input", { class: "input", maxlength: "200", placeholder: "Senior Backend Engineer" });
  const description = h("textarea", { class: "input", rows: "10", placeholder: "Paste the job description here, then draft with AI or fill the fields in yourself." });
  const introPitch = h("textarea", { class: "input", rows: "4", placeholder: "How the AI opens the call" });
  const maxMinutes = h("input", { class: "input", type: "number", min: "1", max: "60", step: "1", value: "15", style: "width:120px" });
  const error = h("p", { class: "error" });
  error.hidden = true;

  const rubric = [];
  const questions = [];
  let faq = [];
  const rubricRows = h("div", {});
  const questionRows = h("div", {});
  const faqNote = h("p", { class: "hint" });

  function drawRubric() {
    rubricRows.replaceChildren(...(rubric.length
      ? rubric.map((d, i) =>
          h("div", { class: "actions", style: "margin-bottom:8px" },
            h("input", { class: "input", style: "flex:1 1 160px", placeholder: "Dimension", value: d.dimension, oninput: (e) => (d.dimension = e.target.value) }),
            h("input", { class: "input", type: "number", step: "0.05", min: "0", style: "width:90px", value: d.weight, oninput: (e) => (d.weight = e.target.value) }),
            h("input", { class: "input", style: "flex:2 1 240px", placeholder: "What a strong answer looks like", value: d.description, oninput: (e) => (d.description = e.target.value) }),
            h("button", { class: "btn btn-sm", type: "button", onclick: () => { rubric.splice(i, 1); drawRubric(); } }, "Remove")))
      : [h("p", { class: "hint", style: "margin:0 0 8px" }, "No dimensions yet. The rubric is what scores the call — add at least one.")]));
  }

  function drawQuestions() {
    questionRows.replaceChildren(...(questions.length
      ? questions.map((q, i) =>
          h("div", { class: "rubric-item" },
            h("div", { class: "actions" },
              h("input", { class: "input", style: "width:160px", placeholder: "id", value: q.id, oninput: (e) => (q.id = e.target.value) }),
              h("input", { class: "input", style: "flex:1 1 240px", placeholder: "Question", value: q.text, oninput: (e) => (q.text = e.target.value) }),
              h("button", { class: "btn btn-sm", type: "button", onclick: () => { questions.splice(i, 1); drawQuestions(); } }, "Remove")),
            h("div", { class: "actions", style: "margin-top:8px" },
              h("input", { class: "input", style: "flex:1 1 200px", placeholder: "Probe (optional)", value: q.probe, oninput: (e) => (q.probe = e.target.value) }),
              h("input", { class: "input", style: "flex:1 1 200px", placeholder: "Extract (comma separated)", value: q.extract, oninput: (e) => (q.extract = e.target.value) }),
              h("label", { class: "hint", style: "margin:0;display:flex;align-items:center;gap:6px" },
                h("input", { type: "checkbox", checked: q.required, onchange: (e) => (q.required = e.target.checked) }), "Required"))))
      : [h("p", { class: "hint", style: "margin:0" }, "No questions yet. A plan needs at least one.")]));
  }

  function drawFaqNote() {
    faqNote.hidden = !faq.length;
    faqNote.textContent = `${faq.length} drafted FAQ answers will be saved with this job.`;
  }

  const draftBtn = h("button", { class: "btn", type: "button", onclick: () => draft() }, "Draft with AI");
  const saveBtn = h("button", { class: "btn btn-primary", type: "submit" }, "Create job");

  async function draft() {
    const text = description.value.trim();
    if (!text) return toast("Paste a job description first.", "bad");
    error.hidden = true;
    draftBtn.disabled = true;
    draftBtn.textContent = "Drafting…";

    try {
      const d = await api("POST", tenantPath("/jobs/drafts"), { title: title.value.trim() || "Untitled role", description: text });
      const plan = d.question_plan || {};
      if (!title.value.trim()) title.value = d.title || "";
      rubric.splice(0, rubric.length, ...(d.rubric || []).map((r) => ({
        dimension: r.dimension || "", weight: String(r.weight), description: r.description || "",
      })));
      questions.splice(0, questions.length, ...(plan.questions || []).map((q) => ({
        id: q.id || "", text: q.text || "", probe: q.probe || "", required: Boolean(q.required),
        extract: Array.isArray(q.extract) ? q.extract.join(", ") : (q.extract || ""),
      })));
      introPitch.value = plan.intro_pitch || "";
      maxMinutes.value = String(plan.max_minutes || 15);
      faq = plan.faq || [];
      drawRubric();
      drawQuestions();
      drawFaqNote();
      toast("Draft ready. Review it before saving.", "ok");
    } catch (err) {
      if (err.status === 401) return;
      const text2 = {
        503: "AI drafting isn't configured on the server.",
        502: "The model didn't return a usable plan — fill the fields in yourself.",
      }[err.status] || err.message;
      toast(text2, "bad");
    } finally {
      draftBtn.disabled = false;
      draftBtn.textContent = "Draft with AI";
    }
  }

  async function save() {
    error.hidden = true;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";

    try {
      const created = await api("POST", tenantPath("/jobs"), {
        title: title.value.trim(),
        rubric: rubric.map((d) => ({
          dimension: d.dimension.trim(),
          weight: Number(d.weight),
          description: d.description.trim(),
        })),
        question_plan: {
          intro_pitch: introPitch.value.trim(),
          questions: questions.map((q) => {
            const extract = q.extract.split(",").map((s) => s.trim()).filter(Boolean);
            return {
              id: q.id.trim(),
              text: q.text.trim(),
              probe: q.probe.trim() || null,
              required: q.required,
              extract: extract.length ? extract : null,
            };
          }),
          faq,
          max_minutes: Number(maxMinutes.value),
        },
      });
      toast(`${created.title} is ready. Add candidates to start screening.`, "ok");
      location.hash = `#/jobs/${created.job_id}`;
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Create job";
    }
  }

  render(
    h("a", { class: "crumb", href: "#/jobs" }, "← Jobs"),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "New job"),
        h("div", { class: "sub" }, "The rubric scores the screening call; the question plan drives it."))),
    h("form", { onsubmit: (e) => { e.preventDefault(); save(); } },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Role")),
        h("div", { class: "card-body" },
          h("div", { class: "form", style: "margin:0" },
            h("div", {}, h("label", { class: "field-label" }, "Title"), title),
            h("div", {}, h("label", { class: "field-label" }, "Job description"), description),
            h("div", { class: "actions" }, draftBtn,
              h("span", { class: "hint", style: "margin:0" }, "Drafting fills in the rubric and question plan below. Nothing is saved until you create the job."))))),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Rubric"),
          h("span", { class: "muted" }, "dimension · weight · description")),
        h("div", { class: "card-body" }, rubricRows,
          h("button", { class: "btn btn-sm", type: "button", onclick: () => { rubric.push({ dimension: "", weight: "1", description: "" }); drawRubric(); } }, "Add dimension"))),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Question plan")),
        h("div", { class: "card-body" },
          h("div", { class: "form", style: "margin:0 0 14px" },
            h("div", {}, h("label", { class: "field-label" }, "Intro pitch"), introPitch),
            h("div", {}, h("label", { class: "field-label" }, "Maximum minutes"), maxMinutes)),
          questionRows,
          h("div", { style: "margin-top:14px" },
            h("button", { class: "btn btn-sm", type: "button", onclick: () => { questions.push({ id: "", text: "", probe: "", required: true, extract: "" }); drawQuestions(); } }, "Add question")),
          faqNote)),
      h("div", { class: "actions", style: "margin-top:16px" },
        saveBtn,
        h("button", { class: "btn", type: "button", onclick: () => (location.hash = "#/jobs") }, "Cancel")),
      error));

  drawRubric();
  drawQuestions();
  drawFaqNote();
  title.focus();
}

// ---------------------------------------------------------------- job detail

async function jobView(jobId) {
  loading();
  const job = await api("GET", tenantPath(`/jobs/${jobId}`));
  const rubric = Array.isArray(job.rubric) ? job.rubric : [];
  const plan = job.question_plan || {};
  const questions = plan.questions || [];
  const faq = plan.faq || [];

  const panel = h("div", {});
  let open = "";
  const toggle = (name, build) => () => {
    open = open === name ? "" : name;
    panel.replaceChildren(...(open ? [h("div", { style: "margin-top:16px" }, build())] : []));
  };

  const candidates = can("candidate:write")
    ? h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Candidates")),
        h("div", { class: "card-body" },
          h("div", { class: "actions" },
            h("button", { class: "btn btn-primary", onclick: toggle("add", () => addCandidateForm(job)) }, "Add candidate"),
            h("button", { class: "btn", onclick: toggle("import", () => importCandidatesForm(job)) }, "Import candidates")),
          h("p", { class: "hint" }, "New candidates are shortlisted against this role and appear under Applications."),
          panel))
    : null;

  const providerPanel = h("div", {});
  const openProviders = () => {
    if (providerPanel.firstChild) return providerPanel.replaceChildren();
    const accountId = accountFor(state.org.org_id);
    providerPanel.replaceChildren(h("div", { style: "margin-top:16px" }, accountId
      ? providerSettingsCard(accountId, state.org.org_id, job.job_id)
      : h("p", { class: "hint", style: "margin:0" },
          "Set your account ID on the Providers screen before editing this role's providers.")));
  };

  render(
    h("a", { class: "crumb", href: "#/jobs" }, "← Jobs"),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, job.title), h("div", { class: "sub" }, `Created ${fmtDateTime(job.created_at)}`)),
      h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: route }, "Refresh"))),
    candidates,
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Providers")),
      h("div", { class: "card-body" },
        h("div", { class: "actions" },
          h("button", { class: "btn", onclick: openProviders }, "Provider settings")),
        h("p", { class: "hint" }, "Keys and models for this role only. Blank fields inherit the organization's. Account owners only."),
        providerPanel)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Rubric")),
      h("div", { class: "card-body" }, rubric.length
        ? rubric.map((d) =>
            h("div", { class: "rubric-item" },
              h("div", { class: "label" }, h("span", {}, humanize(d.dimension)), h("span", { class: "muted" }, `weight ${d.weight}`)),
              h("div", { class: "evidence" }, d.description)))
        : h("span", { class: "muted" }, "No rubric dimensions."))),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Questions"),
        h("span", { class: "muted" }, `up to ${plan.max_minutes || "—"} minutes`)),
      plan.intro_pitch ? h("div", { class: "card-body" }, h("p", { class: "summary-text" }, plan.intro_pitch)) : null,
      questions.length
        ? h("table", { class: "table" },
            h("thead", {}, h("tr", {}, ["#", "Question", "Probe", "Required"].map((t) => h("th", {}, t)))),
            h("tbody", {}, questions.map((q, i) =>
              h("tr", {},
                h("td", { class: "num muted" }, String(i + 1)),
                h("td", {}, q.text, h("span", { class: "secondary" }, q.id)),
                h("td", { class: "muted" }, q.probe || "—"),
                h("td", {}, q.required ? "yes" : "no")))))
        : h("div", { class: "empty" }, "No questions in this plan.")),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "FAQ")),
      faq.length
        ? h("div", { class: "card-body" }, h("dl", { class: "kv" }, faq.flatMap((f) => [h("dt", {}, f.q), h("dd", {}, f.a)])))
        : h("div", { class: "empty" }, "No FAQ answers.")));
}

function addCandidateForm(job) {
  const first = h("input", { class: "input", placeholder: "First name", required: true });
  const last = h("input", { class: "input", placeholder: "Last name", required: true });
  const email = h("input", { class: "input", type: "email", placeholder: "name@example.com", required: true });
  const phone = h("input", { class: "input", placeholder: "+15550100 (optional)" });
  const btn = h("button", { class: "btn btn-primary", type: "submit" }, "Add candidate");
  const error = h("p", { class: "error" });
  error.hidden = true;

  async function submit() {
    error.hidden = true;
    btn.disabled = true;
    btn.textContent = "Adding…";
    try {
      const res = await api("POST", tenantPath("/candidates"), {
        first_name: first.value.trim(),
        last_name: last.value.trim(),
        email: email.value.trim(),
        phone_e164: phone.value.trim() || null,
        job_id: job.job_id,
      });
      toast(res.created
        ? "Candidate added and shortlisted for this role."
        : "That candidate was already in this organization — they're on this role now.", "ok");
      for (const field of [first, last, email, phone]) field.value = "";
      first.focus();
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "Add candidate";
    }
  }

  return h("form", { class: "form", style: "margin:0", onsubmit: (e) => { e.preventDefault(); submit(); } },
    h("div", { class: "actions" },
      h("div", { style: "flex:1 1 180px" }, h("label", { class: "field-label" }, "First name"), first),
      h("div", { style: "flex:1 1 180px" }, h("label", { class: "field-label" }, "Last name"), last)),
    h("div", { class: "actions" },
      h("div", { style: "flex:2 1 240px" }, h("label", { class: "field-label" }, "Email"), email),
      h("div", { style: "flex:1 1 180px" }, h("label", { class: "field-label" }, "Phone"), phone)),
    h("div", { class: "actions" }, btn),
    error);
}

// ---------------------------------------------------------------- CSV import

const CSV_MAX_ROWS = 500;
const CSV_COLUMNS = {
  first_name: "first_name", firstname: "first_name", first: "first_name", given_name: "first_name",
  last_name: "last_name", lastname: "last_name", last: "last_name", surname: "last_name", family_name: "last_name",
  name: "name", full_name: "name", candidate: "name", candidate_name: "name",
  email: "email", email_address: "email", e_mail: "email",
  phone_e164: "phone_e164", phone: "phone_e164", phone_number: "phone_e164", mobile: "phone_e164",
};
const CSV_FIELDS = ["first_name", "last_name", "email", "phone_e164"];

function parseCsv(text) {
  const src = String(text).replace(/^\uFEFF/, "");
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;

  for (let i = 0; i < src.length; i += 1) {
    const c = src[i];
    if (quoted) {
      if (c !== '"') field += c;
      else if (src[i + 1] === '"') { field += '"'; i += 1; }
      else quoted = false;
    } else if (c === '"') {
      quoted = true;
    } else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && src[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      field = "";
      row = [];
    } else {
      field += c;
    }
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((v) => v.trim() !== ""));
}

function rowsFromCsv(text) {
  const table = parseCsv(text);
  if (!table.length) return { error: "That file has no rows." };

  const header = table[0].map((cell) => CSV_COLUMNS[cell.trim().toLowerCase().replace(/[\s-]+/g, "_")] || null);
  const columns = [...new Set(header.filter(Boolean))];
  if (!columns.includes("email")) return { error: "The CSV needs an email column." };

  const body = table.slice(1);
  const rows = body.slice(0, CSV_MAX_ROWS).map((cells) => {
    const row = { first_name: "", last_name: "", email: "", phone_e164: "" };
    let whole = "";
    header.forEach((key, i) => {
      const value = (cells[i] || "").trim();
      if (!key) return;
      if (key === "name") whole = value;
      else row[key] = value;
    });
    if (whole && !row.first_name) {
      const cut = whole.indexOf(" ");
      row.first_name = cut === -1 ? whole : whole.slice(0, cut);
      if (!row.last_name) row.last_name = cut === -1 ? "" : whole.slice(cut + 1).trim();
    }
    return row;
  });

  if (!rows.length) return { error: "That file has a header but no candidates." };
  return { rows, columns, truncated: body.length > CSV_MAX_ROWS };
}

function importCandidatesForm(job) {
  const preview = h("div", {});
  const results = h("div", {});
  const importBtn = h("button", { class: "btn btn-primary", type: "button", onclick: () => run() }, "Import candidates");
  let pending = null;

  async function choose(event) {
    preview.replaceChildren();
    results.replaceChildren();
    pending = null;
    const file = event.target.files && event.target.files[0];
    if (!file) return;

    let parsed;
    try {
      parsed = rowsFromCsv(await file.text());
    } catch {
      return toast("That file couldn't be read.", "bad");
    }
    if (parsed.error) return toast(parsed.error, "bad");

    pending = parsed.rows;
    preview.replaceChildren(
      h("p", { class: "hint", style: "margin:0 0 10px" },
        `${parsed.rows.length} rows · mapped ${parsed.columns.join(", ")}` +
        (parsed.truncated ? ` · only the first ${CSV_MAX_ROWS} will be imported` : "")),
      h("table", { class: "table" },
        h("thead", {}, h("tr", {}, CSV_FIELDS.map((f) => h("th", {}, humanize(f))))),
        h("tbody", {}, parsed.rows.slice(0, 10).map((r) =>
          h("tr", {}, CSV_FIELDS.map((f) => h("td", {}, r[f] || "—")))))),
      h("div", { class: "actions", style: "margin-top:12px" }, importBtn));
  }

  async function run() {
    if (!pending || !pending.length) return;
    importBtn.disabled = true;
    importBtn.textContent = "Importing…";
    try {
      const res = await api("POST", tenantPath("/candidates/import"), {
        job_id: job.job_id,
        rows: pending.map((r) => ({
          first_name: r.first_name,
          last_name: r.last_name,
          email: r.email,
          phone_e164: r.phone_e164 || null,
        })),
      });
      results.replaceChildren(
        h("dl", { class: "kv", style: "margin:16px 0 12px" },
          h("dt", {}, "Created"), h("dd", {}, String(res.created)),
          h("dt", {}, "Matched"), h("dd", {}, String(res.matched)),
          h("dt", {}, "Duplicates"), h("dd", {}, String(res.duplicates)),
          h("dt", {}, "Invalid"), h("dd", {}, String(res.invalid))),
        h("table", { class: "table" },
          h("thead", {}, h("tr", {}, ["Row", "Email", "Result", "Reason"].map((t) => h("th", {}, t)))),
          h("tbody", {}, (res.results || []).map((r) =>
            h("tr", {},
              h("td", { class: "num muted" }, String(r.row)),
              h("td", {}, r.email || "—"),
              h("td", {}, badge(r.status)),
              h("td", { class: "muted" }, r.reason || "—"))))));
      toast(`${res.created} created · ${res.matched} matched · ${res.duplicates} duplicate · ${res.invalid} invalid`, res.invalid ? "bad" : "ok");
    } catch (err) {
      if (err.status === 401) return;
      toast(err.message, "bad");
    } finally {
      importBtn.disabled = false;
      importBtn.textContent = "Import candidates";
    }
  }

  return h("div", {},
    h("label", { class: "field-label" }, "CSV file"),
    h("input", { class: "input", type: "file", accept: ".csv,text/csv", style: "padding:6px", onchange: choose }),
    h("p", { class: "hint" }, `Columns: first_name, last_name, email, phone. A single name column is split on the first space. Email is required, up to ${CSV_MAX_ROWS} rows.`),
    preview,
    results);
}

// ---------------------------------------------------------------- provider settings

const PROVIDERS = ["openai", "deepgram", "cartesia", "elevenlabs"];
const SELECTION_FIELDS = [
  ["stt_provider", "Speech-to-text provider"],
  ["stt_model", "Speech-to-text model"],
  ["tts_provider", "Text-to-speech provider"],
  ["tts_model", "Text-to-speech model"],
  ["tts_voice_id", "Voice"],
  ["llm_provider", "LLM provider"],
  ["llm_call_model", "LLM model for calls"],
  ["llm_summary_model", "LLM model for summaries"],
  ["llm_base_url", "LLM base URL"],
];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const providerPath = (accountId, orgId, jobId) =>
  `/accounts/${accountId}/tenants/${orgId}${jobId ? `/jobs/${jobId}` : ""}/provider-settings`;

// The PUT replaces the whole selection and walks the whole credential list, so every field is
// read back out of the form on save. A typed key is read straight off the input into the request
// body and never stored anywhere else.
function providerSettingsForm(accountId, orgId, jobId, settings, inherited, reload) {
  const blank = jobId ? "Inherit from the organization" : "Not set";
  const selection = settings.selection || {};
  const fields = {};

  const selectionRows = SELECTION_FIELDS.map(([key, label]) => {
    const current = selection[key] || "";
    let input;
    if (key.endsWith("_provider")) {
      const options = current && !PROVIDERS.includes(current) ? [...PROVIDERS, current] : PROVIDERS;
      input = h("select", { class: "select" },
        h("option", { value: "" }, blank),
        options.map((p) => h("option", { value: p }, p)));
    } else {
      input = h("input", { class: "input", placeholder: blank });
    }
    input.value = current;
    fields[key] = input;
    return h("div", {},
      h("label", { class: "field-label" }, label),
      input,
      inherited ? h("p", { class: "hint" }, `Organization: ${inherited[key] || "not set"}`) : null);
  });

  const stored = new Map((settings.credentials || []).map((c) => [c.provider, c]));
  const rows = {};
  const credentialRows = PROVIDERS.map((name) => {
    const existing = stored.get(name);
    const key = h("input", {
      class: "input", type: "password", autocomplete: "off",
      placeholder: existing ? "Leave blank to keep the stored key" : "Paste a key",
    });
    const baseUrl = h("input", { class: "input", placeholder: "Base URL (optional)" });
    baseUrl.value = (existing && existing.base_url) || "";
    const remove = h("input", { type: "checkbox", disabled: !existing });
    rows[name] = { key, baseUrl, remove, existing: Boolean(existing) };

    return h("div", { class: "rubric-item" },
      h("div", { class: "label" },
        h("span", {}, name),
        h("span", { class: "muted" }, existing
          ? `ends ${existing.key_last4} · updated ${fmtDateTime(existing.updated_at)}`
          : "no key stored")),
      h("div", { class: "actions", style: "margin-top:6px" },
        h("div", { style: "flex:1 1 220px" }, key),
        h("div", { style: "flex:1 1 220px" }, baseUrl),
        h("label", { class: "hint", style: "margin:0;display:flex;align-items:center;gap:6px" },
          remove, "Delete")));
  });

  function requestBody() {
    const picked = {};
    for (const [key] of SELECTION_FIELDS) picked[key] = fields[key].value.trim() || null;

    const credentials = [];
    for (const name of PROVIDERS) {
      const row = rows[name];
      const base_url = row.baseUrl.value.trim() || null;
      if (row.remove.checked) credentials.push({ provider: name, api_key: "" });
      else if (row.key.value) credentials.push({ provider: name, api_key: row.key.value, base_url });
      else if (row.existing) credentials.push({ provider: name, base_url });
    }
    return { selection: picked, credentials };
  }

  const saveBtn = h("button", { class: "btn btn-primary", type: "submit" }, "Save providers");
  const error = h("p", { class: "error" });
  error.hidden = true;

  async function save() {
    error.hidden = true;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      await api("PUT", providerPath(accountId, orgId, jobId), requestBody());
      for (const name of PROVIDERS) rows[name].key.value = "";
      toast("Provider settings saved.", "ok");
      reload();
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save providers";
    }
  }

  return h("form", { onsubmit: (e) => { e.preventDefault(); save(); } },
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, jobId ? "This role" : "Organization defaults"),
        h("span", { class: "muted" }, jobId ? "blank inherits the organization" : "blank leaves the server default")),
      h("div", { class: "card-body" }, h("div", { class: "grid" }, selectionRows))),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Keys"),
        h("span", { class: "muted" }, "stored encrypted, never shown again")),
      h("div", { class: "card-body" }, credentialRows,
        h("p", { class: "hint" }, "A blank key keeps whatever is already stored. Tick Delete to remove one."))),
    h("div", { class: "actions", style: "margin-top:16px" }, saveBtn),
    error);
}

function providerSettingsCard(accountId, orgId, jobId) {
  const container = h("div", {});

  async function load() {
    container.replaceChildren(h("div", { class: "spinner" }, "Loading…"));
    try {
      const [settings, organization] = await Promise.all([
        api("GET", providerPath(accountId, orgId, jobId)),
        jobId ? api("GET", providerPath(accountId, orgId, null)) : null,
      ]);
      container.replaceChildren(providerSettingsForm(
        accountId, orgId, jobId, settings, organization && organization.selection, load));
    } catch (err) {
      if (err.status === 401) return;
      const text = {
        403: "Provider settings are owner-only. Ask the account owner to register keys and pick models.",
        404: "That account ID doesn't match an organization you administer. Check it on the Providers screen.",
      }[err.status] || err.message;
      container.replaceChildren(h("div", { class: "card" }, h("div", { class: "empty" }, text)));
    }
  }

  load();
  return container;
}

function accountIdCard(orgId, current) {
  const input = h("input", { class: "input", placeholder: "00000000-0000-0000-0000-000000000000" });
  input.value = current;
  const error = h("p", { class: "error" });
  error.hidden = true;

  function submit() {
    const value = input.value.trim();
    if (!UUID_RE.test(value)) {
      error.hidden = false;
      error.textContent = "That doesn't look like an account ID.";
      return;
    }
    rememberAccount(orgId, value);
    providersView();
  }

  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Account ID")),
    h("div", { class: "card-body" },
      h("p", { class: "muted" },
        "Provider keys belong to the account that owns this organization. The API reports the " +
        "account ID when an organization is created; paste it once and this machine remembers it."),
      h("form", { class: "form", onsubmit: (e) => { e.preventDefault(); submit(); } },
        h("div", {}, h("label", { class: "field-label" }, "Account ID"), input),
        h("div", { class: "actions" }, h("button", { class: "btn btn-primary", type: "submit" }, "Continue")),
        error)));
}

function providersView() {
  const orgId = state.org.org_id;
  const accountId = accountFor(orgId);

  const head = (...tools) => h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Providers"),
      h("div", { class: "sub" }, `Speech and model vendors for ${state.org.org_name}`)),
    h("div", { class: "toolbar" }, tools));

  if (!accountId) return render(head(), accountIdCard(orgId, ""));

  render(
    head(
      h("button", { class: "btn btn-sm btn-ghost", onclick: () => render(head(), accountIdCard(orgId, accountId)) }, "Change account ID"),
      h("button", { class: "btn", onclick: route }, "Refresh")),
    providerSettingsCard(accountId, orgId, null));
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
      h("tr", {}, h("td", { colspan: "5" }, rows.length
        ? h("div", { class: "empty" }, "No applications match your search.")
        : h("div", { class: "empty" },
            h("p", { style: "margin:0 0 12px" }, "No applications yet. Create a job, then add or import candidates against it."),
            can("job:write")
              ? h("button", { class: "btn btn-primary", onclick: () => (location.hash = "#/jobs/new") }, "Create your first job")
              : h("button", { class: "btn", onclick: () => (location.hash = "#/jobs") }, "Browse jobs")))),
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
