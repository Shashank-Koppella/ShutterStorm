// Experiment plan: generate a dry plan -> inspect -> run. Manual injection is secondary.
import { h, icon, mount, toast } from "../js/dom.js";
import { barList } from "../js/charts.js";
import { SOURCE, TARGET_LABEL, faultTitle, pct, num, ago, plural } from "../js/format.js";
import { viewHead, appState } from "../js/app.js";

let waitingForPlan = false;

function planList(p) {
  return h("ol", { class: "steps" }, (p.items || []).map((i, n) => h("li",
    h("span", { class: "n", text: String(n + 1).padStart(2, "0") }),
    h("div", h("div", { class: "row between" }, h("span", { class: "title", text: faultTitle(i.fault) }), h("span", { class: "pill", text: i.failure_class || "" })),
      h("small", { class: "mono", text: i.fault }), h("p", { class: "ai-text", style: { margin: "6px 0 0" }, text: i.reason })))));
}

function candidateChart(p) {
  const picked = new Set((p.items || []).map((i) => i.fault));
  const all = [...(p.candidates || [])].sort((a, b) => (b.priority || 0) - (a.priority || 0));
  const max = Math.max(1, ...all.map((c) => c.priority || 0));
  const bars = (cands) => barList(cands.map((c) => ({
    name: c.fault, value: c.priority || 0, max, display: num(c.priority, 2),
    cls: picked.has(c.fault) ? "" : "muted",
    title: `${c.fault}: priority ${num(c.priority, 3)} (risk ${num(c.risk, 2)}, novelty ${num(c.novelty, 2)})${c.available ? "" : ` · not available: ${c.unavailable_reason}`}${picked.has(c.fault) ? " · selected" : ""}`,
  })), { label: "Deterministic priority of composed experiments" });
  // Show the selected experiments and the strongest alternatives; the rest behind a disclosure.
  const top = all.filter((c, i) => picked.has(c.fault) || i < 12);
  const rest = all.filter((c) => !top.includes(c));
  return h("div", bars(top),
    rest.length ? h("details", { style: { marginTop: "8px" } }, h("summary", { class: "meta", text: `Show the other ${rest.length} compositions` }), bars(rest)) : null);
}

function compositionGrid(p) {
  // Fault type x target: which compositions the planner had (and which it picked).
  const cands = p.candidates || [];
  const picked = new Set((p.items || []).map((i) => i.fault));
  const types = [["pause", "Freeze"], ["stop", "Stop"], ["restart", "Restart"], ["cpu", "CPU limit"], ["latency", "Delay"], ["loss", "Loss"]];
  const targets = ["server", "database", "redis", "ml"];
  const find = (t, k, spike) => cands.find((c) => c.primary_service === t && (spike ? c.kind === "combo" : c.kind === k) &&
    (!spike || c.fault.endsWith(k === "cpu" ? "cpu_throttle" : k === "loss" ? "packet_loss" : k)));
  const cell = (c) => {
    if (!c) return h("span", { class: "cell na", text: "—" });
    const state = picked.has(c.fault) ? "Picked" : c.available ? "Option" : "Off";
    return h("span", { class: `cell ${picked.has(c.fault) ? "slow" : c.available ? "" : "na"}`, title: `${c.fault}${c.available ? "" : `: ${c.unavailable_reason}`}`, text: state });
  };
  return h("div", { class: "matrix" },
    h("div", { class: "mrow head", style: { gridTemplateColumns: "minmax(120px,1.2fr) repeat(6,minmax(0,1fr))" } },
      h("span", { class: "meta", text: "Target / fault type" }), types.map(([, w]) => h("span", { class: "mcol", text: w }))),
    targets.map((t) => h("div", { class: "mrow", style: { gridTemplateColumns: "minmax(120px,1.2fr) repeat(6,minmax(0,1fr))" } },
      h("span", { text: TARGET_LABEL[t] }), types.map(([k]) => cell(find(t, k, false))))));
}

async function render(ctx, alive) {
  const st = ctx.state.status || {};
  const cat = st.catalog || { faults: [], caps: {}, floors: {} };
  const job = st.job || {};
  const busy = Boolean(job.running);
  const res = await ctx.api.latestPlan();
  if (!alive()) return;
  const p = res.plan;

  const budget = h("select", { "aria-label": "Experiments to plan" }, [3, 4, 5, 6, 7, 8].map((n) => h("option", { value: n, selected: n === 6, text: String(n) })));
  const genBtn = h("button", { type: "button", class: "btn-primary", disabled: busy, onclick: async () => {
    try {
      const r = await ctx.api.dryPlan(Number(budget.value));
      if (r.started === false) return toast("Another job is running; try again when it finishes.");
      waitingForPlan = true;
      toast("Planning requested: the AI is ranking the catalog (nothing is injected).");
      ctx.state.status.job = { ...job, running: true, kind: "plan", label: "plan" };
      render(ctx, alive);
    } catch (e) { toast(`Could not start planning: ${e.message}`); }
  } }, icon("sparkles"), "Generate plan");

  const strict = h("input", { type: "checkbox", id: "strict-run" });
  const runBudget = h("select", { "aria-label": "Experiments in the run" }, [1, 2, 3, 4, 5, 6, 7, 8].map((n) => h("option", { value: n, selected: n === (p?.budget || 6), text: String(n) })));
  const startBtn = h("button", { type: "button", class: "btn-primary", disabled: busy, onclick: async () => {
    if (!confirm(`Start an AI-planned run of up to ${runBudget.value} experiments against the Immich sandbox?\n\nFaults are real (in ${st.mode === "simulated" ? "the SIMULATED sandbox" : "your Docker sandbox"}). Stop and Panic stay available.`)) return;
    try {
      const r = await ctx.api.planRun(Number(runBudget.value), strict.checked);
      if (r.started === false) return toast("Another job is running; it was not started.");
      toast("Run requested. Follow it on Live run.");
      ctx.navigate("live");
    } catch (e) { toast(`Could not start the run: ${e.message}`); }
  } }, icon("play"), "Start AI run");

  const faults = cat.faults || [];
  const opt = (f) => h("option", { value: f.id, text: `${f.id} · ${f.description}` });
  const faultSel = h("select", { "aria-label": "Fault to inject" },
    ["server", "database", "redis", "ml"].map((t) => h("optgroup", { label: `Target: ${TARGET_LABEL[t]}` },
      faults.filter((f) => f.service === t && !f.parts.length && !f.optional).map(opt))),
    h("optgroup", { label: "Traffic spike + another fault" }, faults.filter((f) => f.parts.length).map(opt)),
    h("optgroup", { label: "Manual only (safety policy: never chosen by the AI)" }, faults.filter((f) => f.optional).map(opt)));
  const strictManual = h("input", { type: "checkbox", id: "strict-manual" });
  const injectBtn = h("button", { type: "button", disabled: busy, onclick: async () => {
    if (!confirm(`Inject ${faultSel.value} now?`)) return;
    try {
      const r = await ctx.api.runFault(faultSel.value, strictManual.checked);
      if (r.started === false) return toast("Another job is running; it was not started.");
      toast(`${faultSel.value} requested. Follow it on Live run.`);
      ctx.navigate("live");
    } catch (e) { toast(`Could not start: ${e.message}`); }
  } }, icon("flask-conical"), "Inject fault");

  const caps = cat.caps || {};
  const planPanel = p ? h("div", { class: "panel" },
    h("div", { class: "panel-head" },
      h("div", h("h2", "Latest dry plan"), h("div", { class: "meta", text: `${p.id} · ${ago(p.created)} · budget ${p.budget}` })),
      h("span", { class: `pill ${p.source === "live" || p.source === "cache" ? "ai" : ""}` }, p.source === "live" || p.source === "cache" ? icon("sparkles") : null, `Planner: ${SOURCE[p.source] || p.source}`)),
    p.strategy ? h("p", { class: "ai-text", text: p.strategy }) : null,
    h("div", { class: "split" },
      h("div", h("h3", "Selected, in order"), planList(p)),
      h("div", h("h3", "What the AI could compose"), h("p", { class: "meta", text: "Fault types × the services found in the dependency map. Picked = in this plan; Off = safety policy, missing helper image, or not in the map. Traffic-spike combinations are listed in the priority bars." }), compositionGrid(p),
        h("h3", { style: { marginTop: "16px" }, text: "Priority of each composition" }), h("p", { class: "meta", text: "Deterministic features: 0.55 risk + 0.45 novelty + bonuses. Highlighted = selected by the planner." }), candidateChart(p),
        (p.skipped || []).length ? h("details", { style: { marginTop: "12px" } }, h("summary", { class: "meta", text: `Why ${plural(p.skipped.length, "fault")} were not selected` }),
          h("ul", { class: "meta" }, p.skipped.map((s) => h("li", { text: `${s.fault}: ${s.why}` })))) : null)),
    h("div", { class: "notice" }, icon("info"), h("div", { text: "This is a preview. When a run starts, the AI re-decides before every experiment from what it has observed, so the run may differ from this list." })))
    : h("div", { class: "panel empty" }, icon("list-ordered", "icon lg"), h("h2", "No plan yet"), h("p", { text: "Generate a plan to see what the AI would test and why. Nothing is injected." }));

  mount(ctx.el,
    viewHead({ title: "Experiment plan", lead: "Generate a plan, inspect the AI's reasons, then run. The AI composes each experiment from known fault types and the services found in the dependency map.",
      right: h("div", { class: "row" }, h("label", { class: "inline" }, h("span", { class: "meta", text: "Budget" }), budget), genBtn) }),
    busy && job.kind === "plan" ? h("div", { class: "notice warn" }, icon("clock"), h("div", { text: "Planning in progress… the AI is ranking the catalog. This page updates when it finishes." })) : null,
    planPanel,
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Run it"), h("div", { class: "meta", text: "Scope and rollback settings for the run" }))),
      h("div", { class: "split" },
        h("div", h("dl", { class: "kv" },
          h("dt", "Scope"), h("dd", "4 Immich containers (compose project immich) + labelled helpers only"),
          h("dt", "Rollback caps"), h("dd", `error rate > ${pct(caps.max_error_rate)}, p95 > ${num(caps.max_p95_ms, 0)} ms, blast radius ${caps.blast_radius ? "on" : "off"}, ${caps.rounds} round`),
          h("dt", "AI may tighten to"), h("dd", `${pct(cat.floors?.max_error_rate)} errors, ${num(cat.floors?.max_p95_ms, 0)} ms (never looser than the caps)`),
          h("dt", "Fault duration"), h("dd", `${(cat.duration_bounds || []).join("–")} s, hard TTL ${cat.max_fault_seconds} s`),
          h("dt", "Emergency"), h("dd", "Stop (next safe step) and Panic (undo now) stay in the top bar during the run"))),
        h("div", { class: "stack" },
          h("label", { class: "inline" }, h("span", { text: "Experiments" }), runBudget),
          h("label", { class: "inline" }, strict, h("span", { text: "Strict limits demo (roll back above 10% errors)" })),
          startBtn, busy ? h("p", { class: "meta", text: `Disabled while "${job.label}" is running.` }) : null))),
    h("details", { class: "panel" }, h("summary", h("b", "Manual injection"), h("span", { class: "meta", text: "  · one catalog fault, chosen by you" })),
      h("p", { class: "meta", text: "Same composed catalog the AI chooses from; network faults need the nicolaka/netshoot image." }),
      h("div", { class: "row", style: { marginTop: "12px" } }, faultSel, h("label", { class: "inline" }, strictManual, h("span", { text: "Strict limits" })), injectBtn)),
    job.log ? h("details", { class: "panel" }, h("summary", h("b", "Job output"), h("span", { class: "meta", text: `  · ${job.label}${job.exit !== null && job.exit !== undefined ? ` · exit ${job.exit}` : ""}` })),
      h("pre", { class: "diff", text: job.log })) : null);
  mount(ctx.foot, h("span", { text: "Plans and runs use the same CLI commands, so every safety layer applies." }), h("span", { class: "mono", text: st.mode || "" }));
}

async function tick(ctx) {
  const job = ctx.state.status?.job || {};
  if (waitingForPlan && !job.running) {
    waitingForPlan = false;
    await render(ctx, () => true);
  }
}

export default { title: "Experiment plan", icon: "list-ordered", render, tick };
