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
  created: "ok", matched: "info", duplicate: "warn", invalid: "bad",
  awaiting_resume: "warn", extracting: "info", ready: "ok", rejected: "bad",
  qualified: "ok", self_test: "info", shortlisted: "info",
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

const state = {
  me: null,
  org: null,
  pollTimer: null,
  assistant: { conversationId: null, messages: [], pending: null, busy: false },
};

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

function unwrap(res) {
  if (res.status === 401) {
    showLogin("Your session ended. Please sign in again.");
    throw new ApiError(401, "signed out");
  }
  if (!res.ok) throw new ApiError(res.status, res.data && res.data.detail);
  return res.data;
}

async function api(method, path, body) {
  const res = method === "GET"
    ? await window.aihr.get(path)
    : method === "PUT"
      ? await window.aihr.put(path, body)
      : method === "DELETE"
        ? await window.aihr.del(path)
        : await window.aihr.post(path, body);
  return unwrap(res);
}

async function upload(path, payload) {
  return unwrap(await window.aihr.upload(path, payload));
}

async function openFile(path, name) {
  return unwrap(await window.aihr.openFile(path, name));
}

const readFiles = (list) =>
  Promise.all([...list].map(async (f) => ({ name: f.name, bytes: new Uint8Array(await f.arrayBuffer()) })));

const MAX_RESUME_BYTES = 10 * 1024 * 1024;
const MAX_RESUME_FILES = 20;

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

  const saved = state.me.memberships.find((m) => m.org_id === recall());
  state.org = saved || state.me.memberships[0] || null;
  fillOrgSelect();

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

function fillOrgSelect() {
  const select = $("org-select");
  select.replaceChildren(...state.me.memberships.map((m) => h("option", { value: m.org_id }, m.org_name)));
  if (state.org) {
    select.append(new Option("+ New tenant…", NEW_TENANT));
    select.value = state.org.org_id;
  }
  select.disabled = false;
}

const NAV = new Set(["assistant", "jobs", "applications", "providers", "secrets", "account"]);

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
  setActiveNav(NAV.has(parts[0]) ? parts[0] : "applications");
  try {
    if (parts[0] === "assistant") {
      await assistantView();
    } else if (parts[0] === "providers") {
      await providersView();
    } else if (parts[0] === "account") {
      await accountProvidersView();
    } else if (parts[0] === "secrets") {
      await secretsView();
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
        h("div", { class: "card-head" }, h("h2", {}, "Add candidates")),
        h("div", { class: "card-body" },
          h("div", { class: "actions" },
            h("button", { class: "btn btn-primary", onclick: toggle("resumes", () => uploadResumesForm(job)) }, "Upload resumes"),
            h("button", { class: "btn", onclick: toggle("add", () => addCandidateForm(job)) }, "Add one candidate"),
            h("button", { class: "btn", onclick: toggle("import", () => importCandidatesForm(job)) }, "Import a CSV")),
          h("p", { class: "hint" },
            "Uploading resumes is the quickest path — each PDF becomes a candidate. A CSV import " +
            "creates candidates with no resume yet, and they cannot be screened until one is attached."),
          panel))
    : null;

  const rules = can("job:read") ? await optional(api("GET", tenantPath(`/jobs/${jobId}/shortlist-rules`))) : null;
  const rulesValue = rules && !rules.missing ? rules.rules : null;
  const rankedCard = await jobCandidatesCard(job, rulesValue);
  const rulesCard = can("job:write") ? shortlistRulesCard(job, rulesValue) : null;
  const providerCard = await jobProviderCard(job);

  render(
    h("a", { class: "crumb", href: "#/jobs" }, "← Jobs"),
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, job.title), h("div", { class: "sub" }, `Created ${fmtDateTime(job.created_at)}`)),
      h("div", { class: "toolbar" },
        can("call:create") ? h("button", { class: "btn", onclick: () => startTestCall(job) }, "Test the AI agent") : null,
        h("button", { class: "btn", onclick: route }, "Refresh"))),
    candidates,
    rankedCard,
    rulesCard,
    providerCard,
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

async function startTestCall(job) {
  try {
    const res = await api("POST", tenantPath("/test-calls"), { job_id: job.job_id });
    toast("Opening a test call in your browser — you join as the candidate.", "ok");
    window.open(res.join_url, "_blank");
  } catch (err) {
    if (err.status === 401) return;
    toast(err.status === 503
      ? "The voice service isn't configured on this server."
      : err.message, "bad");
  }
}

async function jobCandidatesCard(job, rules) {
  const first = await optional(api("GET", tenantPath(`/jobs/${job.job_id}/candidates?limit=${PAGE}&offset=0`)));
  if (first && first.missing) {
    return h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Candidates")),
      h("div", { class: "empty" }, "You don't have permission to see scores for this role."));
  }

  let rows = first || [];
  let exhausted = rows.length < PAGE;
  let filter = "";
  let qualifiedOnly = false;
  const selected = new Set();

  const tbody = h("tbody");
  const count = h("div", { class: "sub" });
  const more = h("button", { class: "btn btn-sm", onclick: loadMore }, "Load more");
  const panel = h("div", {});
  const summarize = h("button", { class: "btn btn-primary btn-sm", onclick: runSummary }, "Summarize selected");

  const search = h("input", {
    class: "input", type: "search", placeholder: "Search name, email, stage",
    oninput: (e) => { filter = e.target.value; draw(); },
  });
  const onlyBox = h("input", {
    type: "checkbox",
    onchange: (e) => { qualifiedOnly = e.target.checked; draw(); },
  });

  function visibleRows() {
    const needle = filter.trim().toLowerCase();
    return rows.filter((r) =>
      (!qualifiedOnly || r.qualifies) &&
      (!needle || [r.candidate_name, r.candidate_email, r.stage].some((v) => String(v).toLowerCase().includes(needle))));
  }

  function scoreCell(value) {
    return value === null || value === undefined
      ? h("span", { class: "muted" }, "—")
      : meter(value);
  }

  function draw() {
    const visible = visibleRows();
    count.textContent = `${rows.length}${exhausted ? "" : "+"} candidates, best first`;
    summarize.disabled = selected.size < 2 || selected.size > 10;
    summarize.textContent = selected.size ? `Summarize ${selected.size} selected` : "Summarize selected";

    tbody.replaceChildren(...(visible.length ? visible.map((r) => {
      const box = h("input", {
        type: "checkbox",
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => {
          if (e.target.checked) selected.add(r.application_id);
          else selected.delete(r.application_id);
          draw();
        },
      });
      box.checked = selected.has(r.application_id);
      return h("tr", {
        class: r.qualifies ? "clickable qualified" : "clickable",
        onclick: () => (location.hash = `#/applications/${r.application_id}`),
      },
        h("td", { style: "width:32px" }, box),
        h("td", { class: "num muted" }, String(r.rank)),
        h("td", {}, r.candidate_name, h("span", { class: "secondary" }, r.candidate_email)),
        h("td", {}, badge(r.stage)),
        h("td", { style: "width:150px" }, scoreCell(r.resume_score)),
        h("td", { style: "width:150px" }, scoreCell(r.call_score)),
        h("td", { class: "num muted" }, fmtDate(r.created_at)));
    }) : [
      h("tr", {}, h("td", { colspan: "7" }, h("div", { class: "empty" },
        rows.length
          ? "No candidates match."
          : "No candidates yet. Upload resumes above and they'll be scored and ranked here."))),
    ]));
    more.hidden = exhausted || Boolean(filter.trim()) || qualifiedOnly;
  }

  async function loadMore() {
    more.disabled = true;
    try {
      const next = await api("GET", tenantPath(`/jobs/${job.job_id}/candidates?limit=${PAGE}&offset=${rows.length}`));
      rows = rows.concat(next);
      exhausted = next.length < PAGE;
      draw();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      more.disabled = false;
    }
  }

  async function runSummary() {
    summarize.disabled = true;
    summarize.textContent = "Summarising…";
    try {
      const res = await api("POST", tenantPath(`/jobs/${job.job_id}/candidate-summary`), {
        application_ids: [...selected],
      });
      panel.replaceChildren(summaryPanel(res, () => panel.replaceChildren()));
    } catch (err) {
      if (err.status !== 401) {
        toast(err.status === 503 ? "AI summaries aren't configured on this server." : err.message, "bad");
      }
    } finally {
      draw();
    }
  }

  const head = h("div", { class: "card-head" },
    h("h2", {}, "Candidates"),
    h("div", { class: "toolbar" },
      search,
      h("label", { class: "hint", style: "display:flex;align-items:center;gap:6px" }, onlyBox, "Qualified only"),
      can("candidate:write") ? summarize : null));

  const card = h("div", { class: "card" },
    head,
    h("div", { class: "card-body" }, count,
      rules && (rules.top_k || rules.min_score)
        ? h("p", { class: "hint" },
            'Highlighted rows match this role\'s shortlist rules. The "qualified" stage is set once ' +
            "and never removed, so it can outlast a change to the rules.")
        : null),
    h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["", "#", "Candidate", "Stage", "Resume", "Call", "Added"].map((t) => h("th", {}, t)))),
      tbody),
    h("div", { class: "card-body" }, panel),
    h("div", { style: "margin:12px;text-align:center" }, more));

  draw();
  return card;
}

function summaryPanel(res, onClear) {
  return h("div", { class: "card", style: "margin-top:12px" },
    h("div", { class: "card-head" },
      h("h2", {}, "Comparison"),
      h("button", { class: "btn btn-sm", onclick: onClear }, "Clear")),
    h("div", { class: "card-body" },
      h("p", { class: "summary-text" }, res.summary),
      h("div", { class: "grid" }, ...res.candidates.map((c) =>
        h("div", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, c.name)),
          h("div", { class: "card-body" },
            h("div", { class: "field-label" }, "Strengths"),
            c.strengths.length
              ? h("ul", { class: "list" }, ...c.strengths.map((s) => h("li", {}, s)))
              : h("p", { class: "muted" }, "None recorded."),
            h("div", { class: "field-label" }, "Gaps"),
            c.gaps.length
              ? h("ul", { class: "list" }, ...c.gaps.map((s) => h("li", {}, s)))
              : h("p", { class: "muted" }, "None recorded."))))),
      h("div", { class: "field-label" }, "Recommendation"),
      h("p", { class: "summary-text" }, res.recommendation)));
}

function shortlistRulesCard(job, rules) {
  const current = rules || {};
  const topK = h("input", {
    class: "input", type: "number", min: "1", max: "1000", placeholder: "off",
    value: current.top_k == null ? "" : String(current.top_k),
  });
  const minScore = h("input", {
    class: "input", type: "number", min: "0", max: "100", placeholder: "off",
    value: current.min_score == null ? "" : String(Math.round(current.min_score * 100)),
  });
  const combine = h("select", { class: "input" },
    h("option", { value: "all" }, "match all rules"),
    h("option", { value: "any" }, "match any rule"));
  combine.value = current.combine || "all";
  const advance = h("input", { type: "checkbox" });
  advance.checked = current.auto_advance !== false;

  const save = h("button", { class: "btn btn-primary" }, "Save rules");
  const error = h("p", { class: "error" });
  error.hidden = true;

  save.addEventListener("click", async () => {
    error.hidden = true;
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      const body = {
        rules: {
          top_k: topK.value === "" ? null : Number(topK.value),
          min_score: minScore.value === "" ? null : Number(minScore.value) / 100,
          combine: combine.value,
          auto_advance: advance.checked,
        },
      };
      const res = await api("PUT", tenantPath(`/jobs/${job.job_id}/shortlist-rules`), body);
      toast(`Rules saved. ${res.advanced} candidate(s) advanced to qualified.`, "ok");
      route();
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
    } finally {
      save.disabled = false;
      save.textContent = "Save rules";
    }
  });

  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Shortlist rules")),
    h("div", { class: "card-body" },
      h("div", { class: "actions" },
        h("div", { style: "flex:1 1 160px" }, h("label", { class: "field-label" }, "Top K"), topK),
        h("div", { style: "flex:1 1 160px" }, h("label", { class: "field-label" }, "Minimum score (%)"), minScore),
        h("div", { style: "flex:1 1 200px" }, h("label", { class: "field-label" }, "Combine"), combine)),
      h("label", { class: "hint", style: "display:flex;align-items:center;gap:6px;margin:8px 0" },
        advance, 'Advance matching candidates to "qualified"'),
      h("p", { class: "hint" },
        'Matching candidates are highlighted in the list and advanced to "qualified". Advancement ' +
        "is one-way — changing or clearing these rules never moves anyone back, so a candidate can " +
        "stay \"qualified\" after they stop matching. Screenings are never started automatically."),
      h("div", { class: "actions" }, save),
      error));
}

function addCandidateForm(job) {
  const first = h("input", { class: "input", placeholder: "First name", required: true });
  const last = h("input", { class: "input", placeholder: "Last name", required: true });
  const email = h("input", { class: "input", type: "email", placeholder: "name@example.com", required: true });
  const phone = h("input", { class: "input", placeholder: "+15550100 (optional)" });
  const resume = h("input", { class: "input", type: "file", accept: "application/pdf,.pdf", required: true });
  const btn = h("button", { class: "btn btn-primary", type: "submit" }, "Add candidate");
  const error = h("p", { class: "error" });
  error.hidden = true;

  function fail(message) {
    error.hidden = false;
    error.textContent = message;
  }

  async function submit() {
    error.hidden = true;
    const picked = resume.files && resume.files[0];
    if (!picked) return fail("Attach the candidate's resume as a PDF.");
    if (picked.size > MAX_RESUME_BYTES) return fail("That resume is larger than 10 MB.");

    btn.disabled = true;
    btn.textContent = "Adding…";
    try {
      const res = await upload(tenantPath("/candidates"), {
        field: "file",
        fields: {
          first_name: first.value.trim(),
          last_name: last.value.trim(),
          email: email.value.trim(),
          phone_e164: phone.value.trim() || null,
          job_id: job.job_id,
        },
        files: await readFiles([picked]),
      });
      toast(res.created
        ? "Candidate added — reading their resume now."
        : "That candidate was already here; their new resume is being read.", "ok");
      for (const field of [first, last, email, phone, resume]) field.value = "";
      first.focus();
    } catch (err) {
      if (err.status === 401) return;
      fail(err.message);
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
    h("div", {}, h("label", { class: "field-label" }, "Resume (PDF, required)"), resume),
    h("p", { class: "hint" }, "The resume is read and scored against this role automatically."),
    h("div", { class: "actions" }, btn),
    error);
}

function uploadResumesForm(job) {
  const picker = h("input", { class: "input", type: "file", accept: "application/pdf,.pdf", multiple: true });
  const btn = h("button", { class: "btn btn-primary", type: "submit" }, "Upload resumes");
  const error = h("p", { class: "error" });
  const tbody = h("tbody");
  const table = h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["File", "Status", "Detail"].map((t) => h("th", {}, t)))),
    tbody);
  error.hidden = true;
  table.hidden = true;

  let watching = [];

  function draw(rows) {
    tbody.replaceChildren(...rows.map((r) =>
      h("tr", {},
        h("td", {}, r.filename),
        h("td", {}, badge(r.status)),
        h("td", { class: "muted" }, r.reason || r.error_detail || "—"))));
    table.hidden = rows.length === 0;
  }

  async function poll() {
    try {
      const rows = await api("GET", tenantPath(`/jobs/${job.job_id}/resumes`));
      const mine = rows.filter((r) => watching.includes(r.resume_id));
      if (mine.length) draw(mine);
      if (mine.every((r) => r.status === "ready" || r.status === "failed")) {
        stopPolling();
        toast("Resumes processed. They are ranked below.", "ok");
      }
    } catch (err) {
      stopPolling();
    }
  }

  async function submit() {
    error.hidden = true;
    const files = [...(picker.files || [])];
    if (!files.length) {
      error.hidden = false;
      error.textContent = "Choose at least one PDF.";
      return;
    }
    if (files.length > MAX_RESUME_FILES) {
      error.hidden = false;
      error.textContent = `At most ${MAX_RESUME_FILES} resumes per upload.`;
      return;
    }
    const big = files.find((f) => f.size > MAX_RESUME_BYTES);
    if (big) {
      error.hidden = false;
      error.textContent = `${big.name} is larger than 10 MB.`;
      return;
    }

    btn.disabled = true;
    btn.textContent = "Uploading…";
    try {
      const res = await upload(tenantPath("/candidates/resumes"), {
        field: "files",
        fields: { job_id: job.job_id },
        files: await readFiles(files),
      });
      draw(res.resumes);
      watching = res.resumes.map((r) => r.resume_id).filter(Boolean);
      picker.value = "";
      toast(`${res.accepted} resume(s) accepted, ${res.rejected} rejected.`, res.rejected ? "warn" : "ok");
      stopPolling();
      if (watching.length) state.pollTimer = setInterval(poll, 8000);
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "Upload resumes";
    }
  }

  return h("form", { class: "form", style: "margin:0", onsubmit: (e) => { e.preventDefault(); submit(); } },
    h("div", {}, h("label", { class: "field-label" }, "Resume PDFs"), picker),
    h("p", { class: "hint" },
      `Each PDF becomes a candidate on this role. Name, email, phone and links are read from the resume, ` +
      `so no spreadsheet is needed. Up to ${MAX_RESUME_FILES} files, 10 MB each.`),
    h("div", { class: "actions" }, btn),
    error,
    table);
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

// ---------------------------------------------------------------- providers

const SLOTS = [
  ["stt", "Speech-to-text", "Transcribes the candidate."],
  ["tts", "Text-to-speech", "The interviewer's voice."],
  ["llm_call", "Conversation", "Runs the interview live. Favour a fast model."],
  ["llm_summary", "Rubric scoring", "Writes the summary afterwards. Favour a stronger model."],
];
const CUSTOM = "__custom__";
const INHERIT = "";

let catalogue = null;
async function loadCatalogue() {
  if (!catalogue) catalogue = await api("GET", "/provider-catalogue");
  return catalogue;
}

const accountPath = (suffix) => `/accounts/${state.org.account_id}${suffix}`;

function picker(options, { value, blank }) {
  const sel = h("select", { class: "select" });
  sel.append(new Option(blank, INHERIT));
  for (const o of options) sel.append(new Option(o.label, o.value));
  sel.append(new Option("Custom…", CUSTOM));

  const custom = h("input", { class: "input", style: "margin-top:7px", placeholder: "Type a value" });
  custom.hidden = true;

  const known = options.some((o) => o.value === value);
  if (value && !known) {
    sel.value = CUSTOM;
    custom.hidden = false;
    custom.value = value;
  } else {
    sel.value = value || INHERIT;
  }

  sel.addEventListener("change", () => {
    custom.hidden = sel.value !== CUSTOM;
    if (!custom.hidden) custom.focus();
  });

  return {
    node: h("div", {}, sel, custom),
    sel,
    read: () => (sel.value === CUSTOM ? custom.value.trim() || null : sel.value || null),
    onChange: (fn) => { sel.addEventListener("change", fn); custom.addEventListener("input", fn); },
  };
}

function slotEditor(slot, label, why, stored, secrets, inherited, blank) {
  const entry = catalogue[slot] || {};
  const providerNames = Object.keys(entry);
  const badge = h("span", { class: "badge info" }, "Overridden");
  badge.hidden = true;

  const provider = picker(
    providerNames.map((name) => ({ value: name, label: entry[name].label })),
    { value: stored.provider || "", blank });

  const secret = picker(
    secrets.map((s) => ({ value: s.secret_id, label: `${s.name} · ••••${s.last4}` })),
    { value: stored.secret_id || "", blank: blank === "Inherit" ? blank : "Platform default" });

  const baseUrl = h("input", { class: "input", placeholder: "Base URL (optional)" });
  baseUrl.value = stored.base_url || "";

  const valueHost = h("div", {});
  let value = null;

  function fieldFor() {
    const chosen = provider.read() || providerNames[0];
    return entry[chosen] || { field: "model", options: [] };
  }

  function drawValue() {
    const spec = fieldFor();
    const current = spec.field === "voice_id" ? stored.voice_id : stored.model;
    value = picker(spec.options.map((o) => ({ value: o, label: o })), { value: current || "", blank });
    value.onChange(mark);
    valueHost.replaceChildren(
      h("label", { class: "field-label" }, spec.field === "voice_id" ? "Voice" : "Model"),
      value.node);
  }

  function mark() {
    const changed = Boolean(provider.read() || secret.read() || (value && value.read()) || baseUrl.value.trim());
    badge.hidden = !(blank === "Inherit" && changed);
    countOverrides();
  }

  provider.onChange(() => { drawValue(); mark(); });
  baseUrl.addEventListener("input", mark);
  drawValue();

  const node = h("div", { class: "slot" },
    h("div", {},
      h("div", { class: "slot-name" }, label, " ", badge),
      h("div", { class: "slot-why" }, why)),
    h("div", {},
      h("div", { class: "controls" },
        h("div", {}, h("label", { class: "field-label" }, "Provider"), provider.node),
        h("div", {}, h("label", { class: "field-label" }, "Secret"), secret.node),
        valueHost,
        h("div", {}, h("label", { class: "field-label" }, "Base URL"), baseUrl)),
      inherited ? h("p", { class: "hint" }, inheritedLabel(slot, inherited)) : null));

  return {
    node,
    read: () => {
      const spec = fieldFor();
      const picked = value ? value.read() : null;
      return {
        provider: provider.read(),
        secret_id: secret.read(),
        model: spec.field === "model" ? picked : null,
        voice_id: spec.field === "voice_id" ? picked : null,
        base_url: baseUrl.value.trim() || null,
      };
    },
    overridden: () => Boolean(provider.read() || secret.read() || (value && value.read()) || baseUrl.value.trim()),
  };
}

function inheritedLabel(slot, inherited) {
  const from = inherited[slot] || {};
  const parts = [from.provider, from.model || from.voice_id].filter(Boolean);
  return parts.length ? `Inherited: ${parts.join(" · ")}` : "Inherited: platform default";
}

let editors = [];
let overrideCounter = null;
function countOverrides() {
  if (!overrideCounter) return;
  const n = editors.filter((e) => e.overridden()).length;
  overrideCounter.textContent = n === 0 ? "Inheriting everything" : n === 1 ? "1 override" : `${n} overrides`;
}

function selectionFrom(list) {
  const out = {};
  SLOTS.forEach(([slot], i) => (out[slot] = list[i].read()));
  return out;
}

function slotCard(title, note, settings, secrets, inherited, blank, onSave, extraFoot) {
  editors = SLOTS.map(([slot, label, why]) =>
    slotEditor(slot, label, why, (settings.selection || {})[slot] || {}, secrets, inherited, blank));

  overrideCounter = inherited ? h("span", { class: "badge info" }, "Inheriting everything") : null;
  const error = h("p", { class: "error" });
  error.hidden = true;
  const save = h("button", { class: "btn btn-primary" }, "Save");

  save.addEventListener("click", async () => {
    error.hidden = true;
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      await onSave({ selection: selectionFrom(editors) });
      toast("Provider settings saved.", "ok");
      route();
    } catch (err) {
      if (err.status !== 401) { error.hidden = false; error.textContent = err.message; }
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  });

  countOverrides();
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, title), overrideCounter),
    note ? h("div", { class: "card-body" }, h("p", { class: "hint", style: "margin:0" }, note)) : null,
    ...editors.map((e) => e.node),
    h("div", { class: "foot" }, error, h("div", { class: "actions" }, extraFoot || null, save)));
}

async function secretsView() {
  loading();
  await loadCatalogue();
  let rows;
  try {
    rows = await api("GET", accountPath("/secrets"));
  } catch (err) {
    return ownerOnly(err);
  }

  const name = h("input", { class: "input", placeholder: "OpenAI — production" });
  const value = h("input", { class: "input", type: "password", autocomplete: "off", placeholder: "Paste the key" });
  const error = h("p", { class: "error" });
  error.hidden = true;
  const form = h("div", { class: "card" });
  form.hidden = true;

  const add = h("button", { class: "btn btn-primary" }, "New secret");
  add.addEventListener("click", () => {
    form.hidden = !form.hidden;
    add.textContent = form.hidden ? "New secret" : "Cancel";
    if (!form.hidden) name.focus();
  });

  const create = h("button", { class: "btn btn-primary" }, "Save secret");
  create.addEventListener("click", async () => {
    error.hidden = true;
    try {
      await api("POST", accountPath("/secrets"), { name: name.value.trim(), value: value.value });
      value.value = "";
      toast("Secret saved.", "ok");
      route();
    } catch (err) {
      if (err.status !== 401) { error.hidden = false; error.textContent = err.message; }
    }
  });

  form.replaceChildren(
    h("div", { class: "card-head" }, h("h2", {}, "New secret")),
    h("div", { class: "card-body" },
      h("div", { class: "controls" },
        h("div", {}, h("label", { class: "field-label" }, "Name"), name),
        h("div", {}, h("label", { class: "field-label" }, "Value"), value)),
      h("div", { class: "actions", style: "margin-top:12px" }, create),
      error));

  const body = h("tbody");
  const draw = () => body.replaceChildren(...(rows.length ? rows.map(secretRow) : [
    h("tr", {}, h("td", { colspan: "5" }, h("div", { class: "empty" }, "No secrets yet.")))]));

  function secretRow(row) {
    const remove = h("button", { class: "btn btn-sm btn-ghost" }, "Delete");
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api("DELETE", accountPath(`/secrets/${row.secret_id}`));
        toast("Secret deleted.", "ok");
        route();
      } catch (err) {
        remove.disabled = false;
        if (err.status !== 401) toast(err.message, "bad");
      }
    });
    return h("tr", {},
      h("td", {}, h("div", { class: "name" }, row.name)),
      h("td", { class: "num" }, `••••${row.last4}`),
      h("td", {}, row.used_by.length
        ? row.used_by.map((u) => h("span", { class: "badge ok", style: "margin-right:4px" }, u))
        : h("span", { class: "badge warn" }, "Not used yet")),
      h("td", {}, fmtDate(row.created_at)),
      h("td", { style: "text-align:right" }, remove));
  }
  draw();

  render(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Secrets"),
        h("div", { class: "sub" }, "Keys you store once and point providers at.")),
      h("div", { class: "toolbar" }, add)),
    form,
    h("div", { class: "card" },
      h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Name", "Value", "Used by", "Added", ""].map((t) => h("th", {}, t)))),
        body)),
    h("p", { class: "hint" }, "A stored value is never shown again — only replaced or deleted."));
}

function ownerOnly(err) {
  if (err.status === 401) return;
  const text = err.status === 403 || err.status === 404
    ? "Provider settings are owner-only. Ask the account owner to register keys and pick models."
    : err.message;
  render(h("div", { class: "card" }, h("div", { class: "empty" }, text)));
}

async function providersView() {
  loading();
  await loadCatalogue();
  let settings, secrets;
  try {
    [settings, secrets] = await Promise.all([
      api("GET", accountPath(`/tenants/${state.org.org_id}/provider-settings`)),
      api("GET", accountPath("/secrets")),
    ]);
  } catch (err) {
    return ownerOnly(err);
  }

  render(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Providers"),
        h("div", { class: "sub" }, `What ${state.org.org_name} screens with.`))),
    slotCard(
      "Organization defaults",
      "Every job here inherits these. A blank field follows the account.",
      settings, secrets, settings.inherited || {}, "Inherit",
      (body) => api("PUT", accountPath(`/tenants/${state.org.org_id}/provider-settings`), body)),
    h("p", { class: "hint" }, "A single job can override any of these from its own posting."));
}

async function accountProvidersView() {
  loading();
  await loadCatalogue();
  let settings, secrets;
  try {
    [settings, secrets] = await Promise.all([
      api("GET", accountPath("/provider-settings")),
      api("GET", accountPath("/secrets")),
    ]);
  } catch (err) {
    return ownerOnly(err);
  }

  render(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Provider defaults"),
        h("div", { class: "sub" }, "What every organization in the account starts from."))),
    slotCard(
      "Account defaults",
      "An organization or a job overrides these field by field.",
      settings, secrets, null, "Platform default",
      (body) => api("PUT", accountPath("/provider-settings"), body)));
}

async function jobProviderCard(job) {
  await loadCatalogue();
  const host = h("div", {});
  try {
    const data = await api("GET", tenantPath(`/jobs/${job.job_id}/provider-settings`));
    host.replaceChildren(slotCard(
      "Voice and models",
      "Blank fields follow this tenant, then the account.",
      data,
      data.secrets,
      data.inherited,
      "Inherit",
      (body) => api("PUT", tenantPath(`/jobs/${job.job_id}/provider-settings`), body)));
  } catch (err) {
    if (err.status === 403) {
      host.replaceChildren(h("div", { class: "card" }, h("div", { class: "empty" },
        "You don't have permission to change this role's providers.")));
    } else if (err.status !== 401) {
      host.replaceChildren(h("div", { class: "card" }, h("div", { class: "empty" }, err.message)));
    }
  }
  return host;
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

// ---------------------------------------------------------------- assistant

// Model output only ever becomes text nodes. No markdown parser, no innerHTML: the app must
// never parse what the model wrote as markup.
function paragraphs(text) {
  const lines = String(text || "").split("\n");
  return lines.flatMap((line, i) => (i ? [h("br"), line] : [line]));
}

async function assistantView() {
  loading();
  const [history, jobs] = await Promise.all([
    api("GET", tenantPath("/assistant/messages")),
    can("job:read") ? optional(api("GET", tenantPath(`/jobs?limit=${PAGE}&offset=0`))) : null,
  ]);
  const roles = Array.isArray(jobs) ? jobs : [];
  state.assistant = {
    conversationId: history.conversation_id,
    messages: history.messages,
    pending: history.pending_action,
    busy: false,
  };

  const list = h("div", { class: "chat" });
  const box = h("textarea", { class: "input", rows: "3", placeholder: "Ask about candidates for a role…" });
  const send = h("button", { class: "btn btn-primary", type: "submit" }, "Send");
  const panel = h("div", {});

  // Kiran cannot receive bytes, and the model never writes on its own. You pick the files and
  // the role; the app uploads them with your permissions, then tells Kiran what landed.
  const attach = h("input", {
    class: "input", type: "file", accept: "application/pdf,.pdf", multiple: true,
    onchange: () => drawAttach(),
  });
  const roleSelect = h("select", { class: "input" },
    ...roles.map((j) => h("option", { value: j.job_id }, j.title)));
  const attachRow = h("div", { class: "actions" });

  function drawAttach() {
    const count = attach.files ? attach.files.length : 0;
    attachRow.replaceChildren(...(count
      ? [h("div", { style: "flex:1 1 220px" },
           h("label", { class: "field-label" }, `Add ${count} resume(s) to`), roleSelect)]
      : []));
  }

  function bubble(message) {
    const parts = [h("div", { class: "msg-bubble" }, ...paragraphs(message.content))];
    if (message.steps && message.steps.length) {
      parts.push(h("div", { class: "fine" }, `Looked up: ${message.steps.map(humanize).join(", ")}`));
    }
    if (message.fairness_flags && message.fairness_flags.length) {
      parts.push(h("div", { class: "fine" }, badge("flagged"), " flagged for review"));
    }
    return h("div", { class: `msg ${message.role}` }, ...parts);
  }

  function drawPending() {
    const pending = state.assistant.pending;
    if (!pending) return panel.replaceChildren();

    const count = pending.applications.length;
    const confirm = h("button", { class: "btn btn-primary" },
      `Send ${count} invite${count === 1 ? "" : "s"}`);
    const cancel = h("button", { class: "btn" }, "Cancel");

    confirm.addEventListener("click", () => resolveAction(true, confirm, cancel));
    cancel.addEventListener("click", () => resolveAction(false, confirm, cancel));
    if (!can("call:create")) confirm.disabled = true;

    panel.replaceChildren(h("div", { class: "card action-card" },
      h("div", { class: "card-head" }, h("h2", {}, "Confirm before sending")),
      h("div", { class: "card-body" },
        h("p", {}, ...paragraphs(pending.reason)),
        h("table", { class: "table" },
          h("thead", {}, h("tr", {}, ["Candidate", "Role"].map((t) => h("th", {}, t)))),
          h("tbody", {}, pending.applications.map((a) =>
            h("tr", {},
              h("td", {}, a.candidate_name, h("span", { class: "secondary" }, a.candidate_email)),
              h("td", {}, a.job_title))))),
        h("div", { class: "actions" }, confirm, cancel),
        can("call:create")
          ? h("p", { class: "hint" }, "Nothing has been sent yet. Kiran cannot send invites on its own.")
          : h("p", { class: "hint" }, "Your role can't send screening invites."))));
  }

  function draw() {
    list.replaceChildren(...(state.assistant.messages.length
      ? state.assistant.messages.map(bubble)
      : [h("div", { class: "empty" },
          "Ask Kiran who the strongest candidates are for a role, why they rank where they do, " +
          "or to draft screening questions from a job description.")]));
    if (state.assistant.busy) list.appendChild(h("div", { class: "msg assistant" },
      h("div", { class: "msg-bubble muted" }, "Thinking…")));
    drawPending();
    list.scrollTop = list.scrollHeight;
  }

  async function resolveAction(confirmed, confirmBtn, cancelBtn) {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    const id = state.assistant.pending.pending_action_id;
    try {
      const res = await api("POST", tenantPath(`/assistant/actions/${id}/confirm`), { confirm: confirmed });
      state.assistant.pending = null;
      if (confirmed) {
        const started = res.results.filter((r) => r.status === "started").length;
        toast(`Started ${started} of ${res.results.length} screening invite(s).`, "ok");
      } else {
        toast("Proposal cancelled. Nothing was sent.", "ok");
      }
      const refreshed = await api("GET", tenantPath(
        `/assistant/messages?conversation_id=${state.assistant.conversationId}`));
      state.assistant.messages = refreshed.messages;
      state.assistant.pending = refreshed.pending_action;
    } catch (err) {
      if (err.status !== 401) toast(err.message, "bad");
    } finally {
      draw();
    }
  }

  async function uploadAttached() {
    const files = [...(attach.files || [])];
    if (!files.length) return null;
    if (!roles.length) throw new ApiError(400, "Create a role before adding resumes.");
    if (files.length > MAX_RESUME_FILES) {
      throw new ApiError(400, `At most ${MAX_RESUME_FILES} resumes at a time.`);
    }
    const big = files.find((f) => f.size > MAX_RESUME_BYTES);
    if (big) throw new ApiError(400, `${big.name} is larger than 10 MB.`);

    const jobId = roleSelect.value;
    const title = roles.find((j) => j.job_id === jobId)?.title || "the role";
    const res = await upload(tenantPath("/candidates/resumes"), {
      field: "files",
      fields: { job_id: jobId },
      files: await readFiles(files),
    });
    attach.value = "";
    drawAttach();
    return { ...res, title };
  }

  async function submit() {
    const text = box.value.trim();
    const hasFiles = Boolean(attach.files && attach.files.length);
    if ((!text && !hasFiles) || state.assistant.busy) return;

    state.assistant.busy = true;
    send.disabled = true;

    let uploaded = null;
    try {
      uploaded = await uploadAttached();
    } catch (err) {
      state.assistant.busy = false;
      send.disabled = false;
      if (err.status !== 401) toast(err.message, "bad");
      return;
    }

    const prefix = uploaded
      ? `I just added ${uploaded.accepted} resume(s) to "${uploaded.title}"` +
        (uploaded.rejected ? ` (${uploaded.rejected} rejected)` : "") + ". "
      : "";
    const message = (prefix + text).trim() ||
      `${prefix}Once they are read, rank them for that role.`;

    state.assistant.messages = state.assistant.messages.concat([{ role: "user", content: message, steps: [] }]);
    box.value = "";
    draw();

    try {
      const res = await api("POST", tenantPath("/assistant/messages"), {
        message,
        conversation_id: state.assistant.conversationId,
      });
      state.assistant.conversationId = res.conversation_id;
      state.assistant.messages = state.assistant.messages.concat([{
        role: "assistant",
        content: res.text,
        steps: res.steps,
        fairness_flags: res.fairness_flags,
      }]);
      state.assistant.pending = res.pending_action;
    } catch (err) {
      if (err.status !== 401) {
        toast(err.status === 503
          ? "The assistant isn't configured on this server."
          : err.message, "bad");
        // Keep the question in the box so a provider hiccup doesn't lose what they typed.
        // The upload already happened and is not undone; the message says so.
        state.assistant.messages = state.assistant.messages.slice(0, -1);
        box.value = text;
      }
    } finally {
      state.assistant.busy = false;
      send.disabled = false;
      draw();
      box.focus();
    }
  }

  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });

  async function newChat() {
    state.assistant = { conversationId: null, messages: [], pending: null, busy: false };
    draw();
    box.focus();
  }

  async function deleteChat() {
    if (!state.assistant.conversationId) return newChat();
    try {
      await api("DELETE", tenantPath(`/assistant/conversations/${state.assistant.conversationId}`));
      toast("Chat deleted.", "ok");
      newChat();
    } catch (err) {
      if (err.status !== 401) toast(err.message, "bad");
    }
  }

  render(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Assistant"),
        h("div", { class: "sub" }, "Kiran · ranks candidates, explains why, and proposes screenings")),
      h("div", { class: "toolbar" },
        h("button", { class: "btn", onclick: newChat }, "New chat"),
        h("button", { class: "btn", onclick: deleteChat }, "Delete chat"))),
    h("div", { class: "card" }, list),
    panel,
    h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); submit(); } },
      box,
      can("candidate:write") && roles.length
        ? [h("label", { class: "field-label" }, "Attach resumes (optional)"), attach, attachRow]
        : null,
      h("div", { class: "actions" }, send)));

  draw();
  box.focus();
}

function resumeCardFor(app, resume) {
  const head = h("div", { class: "card-head" },
    h("h2", {}, "Resume"),
    resume ? badge(resume.status) : badge("awaiting_resume"));

  if (!resume) {
    return h("div", { class: "card" }, head,
      h("div", { class: "card-body" },
        h("p", { class: "muted" }, "No resume on file yet."),
        attachResumeForm(app, false)));
  }

  if (resume.status === "failed") {
    return h("div", { class: "card" }, head,
      h("div", { class: "card-body" },
        h("p", { class: "error" }, resume.error_detail || "This resume could not be read."),
        h("p", { class: "hint" },
          "Scanned or image-only PDFs have no selectable text. Upload a text PDF instead."),
        h("div", { class: "actions" }, viewPdfButton(app)),
        attachResumeForm(app, true)));
  }

  const data = resume.data || {};
  const links = data.links || {};
  const linkEntries = Object.entries(links)
    .filter(([key, value]) => key !== "other" && value)
    .concat((links.other || []).map((url) => ["link", url]));

  return h("div", { class: "card" }, head,
    h("div", { class: "card-body" },
      h("dl", { class: "kv" },
        h("dt", {}, "Experience"),
        h("dd", {}, data.total_years_experience == null ? "—" : `${data.total_years_experience} years`),
        h("dt", {}, "File"), h("dd", {}, resume.filename)),
      (data.skills || []).length
        ? [h("div", { class: "field-label" }, "Skills"),
           h("p", {}, (data.skills || []).join(", "))]
        : null,
      (data.experience || []).length
        ? [h("div", { class: "field-label" }, "History"),
           h("div", {}, (data.experience || []).map((e) =>
             h("div", { class: "rubric-item" },
               h("div", { class: "label" },
                 h("span", {}, `${e.title || "Role"} · ${e.company || "—"}`),
                 h("span", { class: "muted" }, `${e.start || "?"} – ${e.end || "now"}`)),
               e.summary ? h("div", { class: "evidence" }, e.summary) : null)))]
        : null,
      (data.education || []).length
        ? [h("div", { class: "field-label" }, "Education"),
           h("ul", { class: "list" }, ...(data.education || []).map((e) =>
             h("li", {}, [e.degree, e.institution, e.year].filter(Boolean).join(", "))))]
        : null,
      linkEntries.length
        ? [h("div", { class: "field-label" }, "Links"),
           h("p", {}, ...linkEntries.map(([key, url], i) =>
             h("span", {}, i ? " · " : "", h("a", { href: url }, humanize(key)))))]
        : null,
      h("div", { class: "actions" }, viewPdfButton(app)),
      attachResumeForm(app, true)));
}

function attachResumeForm(app, hasResume) {
  if (!can("candidate:write")) return null;

  const picker = h("input", { class: "input", type: "file", accept: "application/pdf,.pdf" });
  const btn = h("button", { class: "btn btn-primary btn-sm", type: "submit" },
    hasResume ? "Replace resume" : "Attach resume");
  const error = h("p", { class: "error" });
  error.hidden = true;

  async function submit() {
    error.hidden = true;
    const picked = picker.files && picker.files[0];
    if (!picked) {
      error.hidden = false;
      error.textContent = "Choose a PDF first.";
      return;
    }
    if (picked.size > MAX_RESUME_BYTES) {
      error.hidden = false;
      error.textContent = "That resume is larger than 10 MB.";
      return;
    }

    btn.disabled = true;
    btn.textContent = "Uploading…";
    try {
      await upload(tenantPath(`/candidates/${app.candidate_id}/resume`), {
        field: "file",
        fields: { job_id: app.job_id },
        files: await readFiles([picked]),
      });
      toast("Resume uploaded — reading and re-scoring it now.", "ok");
      route();
    } catch (err) {
      if (err.status === 401) return;
      error.hidden = false;
      error.textContent = err.message;
      btn.disabled = false;
      btn.textContent = hasResume ? "Replace resume" : "Attach resume";
    }
  }

  return h("form", { class: "form", style: "margin:12px 0 0", onsubmit: (e) => { e.preventDefault(); submit(); } },
    h("label", { class: "field-label" }, hasResume ? "Replace with a newer PDF" : "Attach a resume PDF"),
    picker,
    h("p", { class: "hint" }, hasResume
      ? "The newest resume is the one used for scoring and screening."
      : "Screening unlocks once a resume has been read."),
    h("div", { class: "actions" }, btn),
    error);
}

function viewPdfButton(app) {
  const btn = h("button", { class: "btn btn-sm" }, "View PDF");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await openFile(
        tenantPath(`/candidates/${app.candidate_id}/resume/file`),
        `${app.candidate_name}.pdf`,
      );
    } catch (err) {
      if (err.status !== 401) toast(err.message, "bad");
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

async function applicationView(applicationId) {
  loading();
  const app = await api("GET", tenantPath(`/applications/${applicationId}`));
  const active = app.calls.some((c) => ACTIVE_STATUSES.has(c.status));
  const resume = can("candidate:read")
    ? await optional(api("GET", tenantPath(`/candidates/${app.candidate_id}/resume`)))
    : null;
  const resumeStatus = resume && !resume.missing ? resume.status : "awaiting_resume";
  const screenable = resumeStatus === "ready";

  const inviteBtn = h("button", {
    class: "btn btn-primary",
    onclick: () => sendInvite(app, inviteBtn),
    disabled: active || !screenable,
  }, "Send email invite");
  const actions = can("call:create")
    ? h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Screening")),
        h("div", { class: "card-body" },
          h("div", { class: "actions" },
            inviteBtn,
            h("button", { class: "btn", disabled: true, title: "Phone screening (Twilio/SIP) is not available yet" }, "Phone call · coming soon")),
          h("p", { class: "hint" }, !screenable
            ? (resumeStatus === "extracting"
                ? "Their resume is still being read. Screening unlocks once it is ready."
                : "This candidate has no extracted resume yet. Attach one before screening.")
            : active
              ? "A screening is in progress for this candidate. This page refreshes on its own."
              : `Emails ${app.candidate_email} a personal link to take the AI screening call in their browser. The link works for 72 hours and is re-sent if unused.`)))
    : null;

  const resumeCard = resumeCardFor(app, resume && !resume.missing ? resume : null);

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
    resumeCard,
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
const NEW_TENANT = "__new_tenant__";

async function createTenant() {
  const name = prompt("Name the new tenant");
  if (!name || !name.trim()) return false;
  try {
    const created = await api("POST", `/accounts/${state.org.account_id}/tenants`, { name: name.trim() });
    state.me = await api("GET", "/me");
    state.org = state.me.memberships.find((m) => m.org_id === created.org_id) || state.org;
    remember(state.org.org_id);
    fillOrgSelect();
    toast(`${created.name} is ready.`, "ok");
    return true;
  } catch (err) {
    if (err.status !== 401) toast(err.message, "bad");
    return false;
  }
}

$("org-select").addEventListener("change", async (e) => {
  if (e.target.value === NEW_TENANT) {
    if (!(await createTenant())) fillOrgSelect();
    location.hash = "#/applications";
    return route();
  }
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
