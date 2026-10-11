// ShutterStorm dashboard (Stormglass). Shell, routing, status polling, emergency controls.
import { api } from "./api.js";
import { h, icon, mount, toast, announce } from "./dom.js";
import { runDate, num, ago, plural } from "./format.js";
import results from "../views/results.js";
import live from "../views/live.js";
import plan from "../views/plan.js";
import decisions from "../views/decisions.js";
import safety from "../views/safety.js";
import graph from "../views/graph.js";
import history from "../views/history.js";

const VIEWS = { results, live, plan, decisions, safety, graph, history };
const ORDER = ["results", "live", "plan", "decisions", "safety", "graph", "history"];
const POLL_MS = 1500;

const state = {
  status: null, runs: [], mode: null, conn: { ok: true, last: 0, error: "" },
  runCache: new Map(), panicRequestedAt: null, lastJobKey: null, view: null, params: {},
};

// ------------------------------------------------------------------ theme
const THEMES = ["system", "light", "dark"];
function storedTheme() { try { return localStorage.getItem("ss-theme") || "system"; } catch { return "system"; } }
function applyTheme(t) {
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("ss-theme", t); } catch { /* storage unavailable */ }
}
const urlTheme = new URLSearchParams(location.search).get("theme");
applyTheme(THEMES.includes(urlTheme) ? urlTheme : storedTheme());

// ------------------------------------------------------------------ routing
function parseHash() {
  const [path, query] = (location.hash.replace(/^#\/?/, "") || "results").split("?");
  const view = ORDER.includes(path) ? path : "results";
  return { view, params: Object.fromEntries(new URLSearchParams(query || "")) };
}
export function navigate(view, params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
  location.hash = `#/${view}${q ? `?${q}` : ""}`;
}
export function setParams(patch) {
  navigate(state.view, { ...state.params, ...patch });
}

// ------------------------------------------------------------------ data
export async function getRun(id, { fresh = false } = {}) {
  if (!id) return null;
  if (!fresh && state.runCache.has(id)) return state.runCache.get(id);
  const run = await api.run(id);
  state.runCache.set(id, run);
  return run;
}
export function currentRunId() {
  const id = state.params.run;
  if (id && state.runs.some((r) => r.id === id)) return id;
  return state.runs[0]?.id || null;
}
export const jobRunning = () => Boolean(state.status?.job?.running);
export const appState = state;

async function refreshRuns() {
  try {
    const res = await api.runs();
    state.runs = res.runs;
    state.mode = res.mode;
  } catch { /* status poll reports connectivity */ }
}

// ------------------------------------------------------------------ shell
const els = {};
function buildShell() {
  const app = document.getElementById("app");
  const nav = h("nav", { class: "nav", "aria-label": "Screens" }, ORDER.map((key) =>
    h("a", { href: `#/${key}`, "data-view": key }, icon(VIEWS[key].icon), VIEWS[key].title)));
  els.railMode = h("p", { class: "mono", text: "…" });
  const rail = h("aside", { class: "rail" },
    h("div", { class: "brand" }, h("span", { class: "logo", "aria-hidden": "true", text: "S" }), "ShutterStorm"),
    nav,
    h("div", { class: "railnote" },
      h("span", { class: "tag", text: "Local sandbox" }), h("p", "Immich", h("br"), "4 scoped services"),
      h("hr"), h("span", { class: "tag", text: "Data" }), els.railMode,
      h("hr"), h("p", "Stormglass", h("br"), "ShutterStorm dashboard")));
  els.nav = nav;
  els.crumb = h("span", { class: "view-name" });
  els.conn = h("span", { class: "conn", role: "status" });
  els.svcs = h("span", { class: "row", "aria-label": "Sandbox containers" });
  els.job = h("span");
  els.stop = h("button", { type: "button", onclick: onStop, title: "Stop the running workflow at its next safe step" }, icon("square"), "Stop");
  els.panic = h("button", { type: "button", class: "btn-danger", onclick: onPanic, title: "Undo every active fault immediately" }, icon("octagon-x"), "Panic");
  els.theme = h("button", { type: "button", class: "btn-ghost", onclick: cycleTheme, "aria-label": "Switch colour appearance" }, icon("sun"), h("span", { text: "Appearance" }));
  const topbar = h("div", { class: "topbar" },
    h("div", { class: "crumbs" }, "Workspace / Immich resilience / ", els.crumb),
    h("div", { class: "right" }, els.conn, els.svcs, els.job, h("span", { class: "emergency" }, els.stop, els.panic), els.theme));
  els.banners = h("div");
  els.view = h("div", { id: "view" });
  els.foot = h("div", { class: "foot" });
  const main = h("main", { class: "main", id: "main" }, topbar, els.banners, els.view, els.foot);
  mount(app, rail, main);
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
  applyTheme(next);
  announce(`Appearance: ${next}`);
  updateTopbar();
}

function updateTopbar() {
  const st = state.status;
  const now = Date.now() / 1000;
  els.conn.className = `conn ${state.conn.ok ? "" : "stale"}`;
  mount(els.conn, h("span", { class: "dot", "aria-hidden": "true" }),
    state.conn.ok ? "Connected" : `Stale · last update ${state.conn.last ? ago(state.conn.last, now) : "never"}`,
    state.conn.ok ? null : h("button", { type: "button", class: "btn-ghost", onclick: poll, style: { minHeight: "24px", padding: "0 6px" } }, "Retry"));
  els.theme.lastChild.textContent = `Appearance: ${storedTheme()}`;
  if (!st) return;
  mount(els.svcs, st.containers.map((c) => {
    const name = c.name.replace("immich_", "").replace("machine_learning", "ml").replace("postgres", "db");
    let cls = "", word = "running";
    if (!c.exists) { cls = "down"; word = "missing"; }
    else if (c.paused) { cls = "paused"; word = "paused"; }
    else if (!c.running) { cls = "down"; word = "stopped"; }
    else if (c.health && c.health !== "healthy") { cls = "unhealthy"; word = c.health; }
    return h("span", { class: `svc ${cls}`, title: `${c.name}: running=${c.running} paused=${c.paused} health=${c.health ?? "none"}` }, h("b", { text: name }), word);
  }));
  const job = st.job || {};
  if (job.running) mount(els.job, h("span", { class: "pill warn" }, icon("activity"), `Running: ${job.label}`));
  else if (state.panicRequestedAt) {
    const clear = st.journal.length === 0 && st.containers.every((c) => c.running && !c.paused);
    mount(els.job, h("span", { class: `pill ${clear ? "ok" : "danger"}` }, icon(clear ? "circle-check" : "undo-2"),
      clear ? "Journal empty · containers running" : "Rollback requested"));
    if (clear && now - state.panicRequestedAt > 60) state.panicRequestedAt = null;
  } else if (st.journal.length) mount(els.job, h("span", { class: "pill danger" }, icon("triangle-alert"), `${plural(st.journal.length, "active fault")}`));
  else mount(els.job);
  els.stop.disabled = !job.running;
  els.railMode.textContent = st.mode === "simulated" ? "SIMULATED (fake Docker + Immich)" : "Real sandbox runs";
  mount(els.banners, st.mode === "simulated"
    ? h("div", { class: "banner", role: "note" }, icon("flask-conical"), "SIMULATED: fake Docker and fake Immich. Nothing here is a real finding.")
    : null,
  st.stop_file && !job.running ? h("div", { class: "notice warn" }, icon("info"), "A STOP file is present (left by an earlier Stop). The next run removes it.") : null);
}

async function onStop() {
  try { await api.stop(); toast("Stop requested: the workflow stops at its next safe step."); }
  catch (e) { toast(`Stop failed: ${e.message}`); }
}
async function onPanic() {
  if (!confirm("Panic: undo every active fault now, remove helpers and restore the checkpoint?\n\nThe running workflow is stopped.")) return;
  try {
    await api.panic();
    state.panicRequestedAt = Date.now() / 1000;
    toast("Rollback requested. Waiting for the journal to empty and containers to run.");
    updateTopbar();
  } catch (e) { toast(`Panic request failed: ${e.message}`); }
}

// ------------------------------------------------------------------ polling
async function poll() {
  try {
    const st = await api.status();
    state.status = st;
    state.conn = { ok: true, last: Date.now() / 1000, error: "" };
    const job = st.job || {};
    const key = `${job.running}|${job.run_id}|${job.exit}|${job.label}`;
    if (key !== state.lastJobKey) {
      const finished = state.lastJobKey !== null && !job.running;
      state.lastJobKey = key;
      await refreshRuns();
      if (job.run_id) state.runCache.delete(job.run_id);
      if (finished) {
        announce(`${job.kind === "plan" ? "Plan" : "Run"} finished${job.exit ? ` with exit code ${job.exit}` : ""}.`);
        rerender();
      }
    }
  } catch (e) {
    state.conn = { ...state.conn, ok: false, error: e.message };
  }
  updateTopbar();
  const view = VIEWS[state.view];
  if (view?.tick) {
    try { await view.tick(ctx()); } catch (e) { console.warn(e); }
  }
}

// ------------------------------------------------------------------ rendering
function ctx() {
  return { el: els.view, state, params: state.params, navigate, setParams, getRun, currentRunId, api, foot: els.foot };
}

let renderToken = 0;
async function rerender() {
  const { view, params } = parseHash();
  state.view = view;
  state.params = params;
  for (const a of els.nav.querySelectorAll("a")) a.setAttribute("aria-current", a.dataset.view === view ? "page" : "false");
  els.crumb.textContent = VIEWS[view].title;
  document.title = `${VIEWS[view].title} · ShutterStorm`;
  const token = ++renderToken;
  mount(els.foot);
  try {
    await VIEWS[view].render(ctx(), () => token === renderToken);
  } catch (e) {
    if (token !== renderToken) return;
    mount(els.view, h("div", { class: "panel" }, h("div", { class: "notice danger" }, icon("circle-x"),
      h("div", h("b", "This screen could not load. "), e.message || String(e)))));
    console.error(e);
  }
}

/** Run picker shared by run-scoped screens. */
export function runPicker(selectedId, { label = "Run" } = {}) {
  if (!state.runs.length) return null;
  const sel = h("select", { "aria-label": `${label} to show`, onchange: (e) => setParams({ run: e.target.value, exp: null }) },
    state.runs.map((r) => h("option", { value: r.id, selected: r.id === selectedId },
      `${runDate(r.id)} · ${r.overall_score === null || r.overall_score === undefined ? "no score" : num(r.overall_score)} · ${r.experiments} exp${r.status === "completed" ? "" : ` · ${r.status}`}`)));
  return h("label", { class: "runpick" }, h("span", { class: "meta", text: label }), sel);
}

/** Header used by most screens: tag line, title, lead, optional right-side controls. */
export function viewHead({ tag, tagClass = "", title, lead, right }) {
  return h("div", { class: "view-head" },
    h("div", tag ? h("span", { class: `tag ${tagClass}`, text: tag }) : null, h("h1", { text: title }), lead ? h("p", { class: "lead", text: lead }) : null),
    right || null);
}

export function emptyState({ title = "No experiments yet", body, actions = [] }) {
  return h("div", { class: "panel empty" }, icon("circle-dashed", "icon lg"), h("h2", { text: title }), body ? h("p", { text: body }) : null,
    actions.length ? h("div", { class: "row", style: { justifyContent: "center" } }, actions) : null);
}

async function start() {
  buildShell();
  await poll();
  await refreshRuns();
  await rerender();
  window.addEventListener("hashchange", rerender);
  setInterval(poll, POLL_MS);
}
start();

