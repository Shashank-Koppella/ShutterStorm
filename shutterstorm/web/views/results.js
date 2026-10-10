// Run results: outcome strip, fault x journey matrix, selected finding evidence, latency strips.
import { h, icon, mount, announce } from "../js/dom.js";
import { latencyStrips, stripsLegend, barList } from "../js/charts.js";
import { JOURNEY_IDS, JOURNEY_SHORT, JOURNEY_LONG, OUTCOME, SOURCE, outcomeClass, faultTitle, num, secs, pct, date, plural } from "../js/format.js";
import { runPicker, viewHead, emptyState, appState } from "../js/app.js";

const COMPONENTS = [["A", "Availability"], ["L", "Latency"], ["C", "Containment"], ["I", "Integrity"], ["R", "Recovery"], ["B", "Fails fast"]];

export function statusTag(run) {
  const cl = run.cleanup;
  const cleanup = cl && cl.checks && cl.checks.length ? (cl.ok ? "Cleanup passed" : "Cleanup FAILED") : "Cleanup not available";
  const status = { completed: "Completed run", aborted: "Aborted run", interrupted: "Interrupted run", error: "Run error" }[run.status] || `Run ${run.status}`;
  const cls = run.status !== "completed" || (cl && cl.checks && cl.checks.length && !cl.ok) ? "danger" : "";
  return { text: `${status} · ${cleanup}`, cls };
}

function metric(value, caption, cls = "", small = "") {
  return h("div", { class: "metric" }, h("div", { class: `value ${cls}` }, value, small ? h("small", ` ${small}`) : null), h("div", { class: "caption", text: caption }));
}

export function outcomeStrip(run) {
  const exps = run.experiments;
  const autos = exps.filter((e) => e.auto_stopped).length;
  const cl = run.cleanup;
  const checks = cl && cl.checks && cl.checks.length ? `${cl.checks.filter((c) => c.ok).length}/${cl.checks.length}` : "—";
  return h("div", { class: "metrics" },
    metric(run.overall_score === null || run.overall_score === undefined ? "—" : num(run.overall_score), "Resilience score (formula, not AI)", "", "/ 100"),
    metric(String(exps.length).padStart(2, "0"), "Experiments completed"),
    metric(String(autos).padStart(2, "0"), autos === 1 ? "Automatic rollback" : "Automatic rollbacks", autos ? "warn" : ""),
    metric(checks, cl && cl.checks && cl.checks.length ? "Cleanup checks passed" : "Cleanup evidence not available", cl && cl.checks && cl.checks.length && !cl.ok ? "danger" : ""));
}

function matrix(run, selected, onSelect) {
  const head = h("div", { class: "mrow head" }, h("span", { class: "meta", text: "Fault / severity order" }),
    JOURNEY_IDS.map((j) => h("span", { class: "mcol", title: JOURNEY_LONG[j] }, h("b", j), JOURNEY_SHORT[j])),
    h("span", { class: "mcol", text: "Score" }));
  const rows = run.experiments.map((e) => {
    const sc = e.scoring || {};
    const btn = h("button", { type: "button", "aria-pressed": String(e.label === selected), onclick: () => onSelect(e.label) },
      e.label, h("span", { class: "sub", text: `${sc.severity_label || "—"} · ${num(sc.severity, 3)}${e.auto_stopped ? " · auto-stopped" : ""}` }));
    return h("div", { class: "mrow" }, btn,
      JOURNEY_IDS.map((j) => {
        const v = (e.heatmap || {})[j] || "n/a";
        return h("span", { class: `cell ${outcomeClass(v)}`, title: `${e.label}, ${j} ${JOURNEY_LONG[j]}: ${OUTCOME[v] || v}` },
          h("span", { class: "jl", text: `${j} ${JOURNEY_SHORT[j]}` }), OUTCOME[v] || v);
      }),
      h("span", { class: "mscore", text: num(sc.score) }));
  });
  return h("div", { class: "matrix", role: "group", "aria-label": "Fault by user journey outcomes" }, head, rows);
}

function sourcePill(src) {
  const label = SOURCE[src] || src || "unknown";
  return h("span", { class: `pill ${src === "live" || src === "cache" ? "ai" : ""}` }, src === "live" || src === "cache" ? icon("sparkles") : null, label);
}

function evidence(run, e) {
  const sc = e.scoring || {};
  const integ = e.integrity || {};
  const dec = e.decision || {};
  const params = Object.entries(e.params || dec.params || {});
  const auto = e.auto_stopped;
  const sug = e.suggestion;
  const limits = (dec.settings && dec.settings.max_error_rate !== undefined)
    ? `${pct(dec.settings.max_error_rate)} errors · ${num(dec.settings.max_p95_ms, 0)} ms p95` : (e.safety || {}).thresholds || run.thresholds || "—";
  const measured = h("section", h("h3", icon("gauge"), "Measured effect"),
    h("dl", { class: "kv" },
      h("dt", "Score"), h("dd", `${num(sc.score)} / 100 (severity ${num(sc.severity, 3)}, ${sc.severity_label || "—"})`),
      h("dt", "Broke"), h("dd", (e.actual_impacted || []).length ? e.actual_impacted.map((j) => `${j} ${JOURNEY_SHORT[j]}`).join(", ") : "nothing"),
      h("dt", "Recovery"), h("dd", e.recovery_s === null || e.recovery_s === undefined ? "did not recover within the limit" : `${secs(e.recovery_s)} after undo`),
      h("dt", "Uploads"), h("dd", integ.attempted ? `${integ.stored}/${integ.attempted} stored, ${integ.intact} intact${integ.ghost_uploads ? `, ${integ.ghost_uploads} ghost` : ""}` : "none during the fault"),
      h("dt", "Fault ran"), h("dd", e.fault_duration_s ? `${secs(e.fault_duration_s)} of a ${secs(e.window_s)} window` : `${secs(e.window_s)} window`),
      params.length ? [h("dt", "Strength"), h("dd", params.map(([k, v]) => `${k} = ${v}`).join(", "))] : null,
      e.load ? [h("dt", "Traffic"), h("dd", `${e.load.sent} requests, ${e.load.requests_per_s}/s, ${e.load.errors} errors`)] : null));
  const safety = h("section", h("h3", icon("shield-check"), "Safety response"),
    auto ? h("div", h("span", { class: "pill warn" }, "Auto-stopped"), h("p", { text: `Rolled back after ${auto.after_s} s: ${auto.reason}` }))
      : h("p", { class: "muted", text: "No threshold was breached; the fault ran its full window and was undone on schedule." }),
    h("dl", { class: "kv" }, h("dt", "Limits"), h("dd", String(limits)),
      h("dt", "Undo"), h("dd", e.undo ? (e.undo.ok ? "verified" : `NOT verified: ${e.undo.detail}`) : "—"),
      dec.source ? [h("dt", "Limits set by"), h("dd", SOURCE[dec.source] || dec.source)] : null),
    (dec.clamped || []).length ? h("p", { class: "meta", text: `Clamped by the controller: ${dec.clamped.join("; ")}` }) : null);
  let remedy;
  if (!sug) remedy = h("section", h("h3", icon("file-text"), "Proposed remediation"), h("p", { class: "muted", text: "No fix suggested: nothing broke in this experiment." }));
  else {
    const text = sug.kind === "config" ? (sug.rationale || sug.summary) : sug.recommendation;
    remedy = h("section", h("h3", icon("file-text"), "Proposed remediation"),
      h("div", { class: "row" }, h("span", { class: "pill warn" }, "Not applied · Not tested"), sourcePill(sug.source)),
      h("p", { class: "ai-text", text: text || "—" }),
      sug.kind === "config" ? h("details", h("summary", { class: "meta", text: sug.valid ? "Config change (uses allowed settings only)" : "Config change (rejected by the checker)" }),
        h("pre", { class: "diff", text: sug.diff || (sug.validation_errors || []).join("\n") })) : null,
      sug.template_code_fix ? h("div", { class: "fixbox" }, h("span", { class: "meta", text: "Code-level fix (template)" }), h("div", { text: sug.template_code_fix })) : null);
  }
  return h("div", { class: "evidence-steps" }, measured, safety, remedy);
}

async function strips(ctx, run, e, slot, alive) {
  if (!e.has_evidence) return mount(slot, h("p", { class: "meta", text: "No per-probe evidence saved for this experiment." }));
  mount(slot, h("div", { class: "skeleton" }));
  try {
    const t = await ctx.api.timeline(run.id, e.label);
    if (!alive()) return;
    mount(slot, latencyStrips(t, { title: `${e.label}: latency by journey` }), stripsLegend(),
      h("p", { class: "meta", text: `Time 0 = ${t.t0_source}. Faded dots are recovery probes after the undo. Each dot is one measured call.` +
        (t.t0_source !== "injection" && t.auto_stop_after_s ? ` This run predates recorded injection times, so the automatic rollback (${t.auto_stop_after_s} s after injection) is not drawn on this axis.` : "") }));
  } catch (err) {
    mount(slot, h("p", { class: "meta", text: `Timeline not available: ${err.message}` }));
  }
}

async function render(ctx, alive) {
  const runId = ctx.currentRunId();
  if (!runId) {
    return mount(ctx.el, viewHead({ title: "Run results" }), emptyState({
      body: "Generate a plan and start a run, or check the environment first with `python -m shutterstorm doctor`.",
      actions: [h("a", { class: "btn btn-primary", href: "#/plan" }, icon("list-ordered"), "Generate plan")] }));
  }
  const run = await ctx.getRun(runId);
  if (!alive()) return;
  const selected = run.experiments.some((e) => e.label === ctx.params.exp) ? ctx.params.exp : run.experiments[0]?.label;
  const tag = statusTag(run);
  const decided = (run.decisions || []).filter((d) => d.source === "live" || d.source === "cache").length;
  const lead = `${date(run.started)} · ${plural(run.experiments.length, "experiment")}` +
    (decided ? ` · ${decided} decided live by the AI` : run.plan?.source === "operator" ? " · chosen by the operator" : "");
  const right = h("div", { class: "row" }, runPicker(run.id),
    run.has_report ? h("a", { class: "btn", href: ctx.api.reportUrl(run.id), target: "_blank", rel: "noopener" }, icon("external-link"), "Full report") : null);
  const evidenceSlot = h("div");
  const stripSlot = h("div");
  const detailSlot = h("div");

  const select = (label) => {
    ctx.state.params.exp = label;
    history.replaceState(null, "", `#/results?run=${encodeURIComponent(run.id)}&exp=${encodeURIComponent(label)}`);
    for (const b of ctx.el.querySelectorAll(".mrow > button")) b.setAttribute("aria-pressed", String(b.firstChild.textContent === label));
    showSelected(label);
    announce(`Selected ${label}`);
  };
  const showSelected = (label) => {
    const e = run.experiments.find((x) => x.label === label);
    if (!e) return;
    const sc = e.scoring || {};
    mount(evidenceSlot, h("div", { class: "panel" },
      h("div", { class: "panel-head" },
        h("div", h("span", { class: "tag", text: `Selected finding · ${e.label}` }), h("h2", { text: faultTitle(e.fault) }),
          h("div", { class: "meta", text: (e.parts || []).length > 1 ? `Combined failure: ${e.parts.join(" + ")}` : e.kind ? `Fault type: ${e.kind}` : "" })),
        h("div", { class: "row" }, h("span", { class: `pill ${sc.severity_label === "high" || sc.severity_label === "critical" ? "danger" : sc.severity_label === "medium" ? "warn" : "ok"}` }, `Severity ${sc.severity_label || "—"}`),
          e.auto_stopped ? h("span", { class: "pill warn" }, "Auto-stopped") : null)),
      evidence(run, e)));
    mount(stripSlot, h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "What users experienced, call by call"), h("div", { class: "meta", text: "Each journey probed concurrently during the fault and after the undo" }))),
      h("div")));
    strips(ctx, run, e, stripSlot.querySelector(".panel").lastChild, alive);
    const an = e.analysis || {};
    const pred = e.prediction || {};
    const pc = e.prediction_check || {};
    mount(detailSlot, h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", h("h2", "Score breakdown"), h("div", { class: "meta", text: "S = 100 × (0.30A + 0.15L + 0.20C + 0.15I + 0.10R + 0.10B)" }))),
        barList(COMPONENTS.map(([k, name]) => {
          const v = (sc.components || {})[k];
          return { name: `${k} · ${name}`, value: v ?? 0, max: 1, display: v === undefined ? "—" : num(v, 2), cls: v < 0.5 ? "danger" : v < 0.85 ? "warn" : "" };
        }), { label: "Score components" }),
        h("p", { class: "meta", text: `Criticality ${sc.criticality ?? "—"} × likelihood ${sc.likelihood ?? "—"} weights the severity.` })),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", h("h2", "AI analysis"), h("div", { class: "meta", text: "Unverified explanation; the measurements above are the evidence" })), sourcePill(an.source)),
        h("p", { class: "ai-text", text: an.summary || "No analysis recorded." }),
        an.root_cause_hypothesis ? h("p", { class: "meta", text: `Root-cause hypothesis: ${an.root_cause_hypothesis}` }) : null,
        h("h3", { text: "Predicted vs measured impact" }),
        h("div", { class: "chips" }, JOURNEY_IDS.map((j) => {
          const p = (pred.predicted_impacted || []).includes(j), a = (e.actual_impacted || []).includes(j);
          if (!p && !a) return null;
          const word = p && a ? "predicted + broke" : p ? "predicted, stayed OK" : "broke, not predicted";
          return h("span", { class: `chip ${a ? "hit" : ""}`, text: `${j} ${JOURNEY_SHORT[j]}: ${word}` });
        })),
        h("p", { class: "meta", text: `Prediction by ${SOURCE[pred.source] || pred.source || "—"}; ${pc.fp?.length || 0} over-predicted, ${pc.fn?.length || 0} missed.` }))));
  };

  mount(ctx.el,
    viewHead({ tag: tag.text, tagClass: tag.cls, title: "Run results", lead, right }),
    run.status !== "completed" && run.error ? h("div", { class: "notice danger" }, icon("triangle-alert"), h("div", { text: `This run ended early: ${run.error}` })) : null,
    outcomeStrip(run),
    run.experiments.length ? h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Where the experience breaks"), h("div", { class: "meta", text: "Select a fault to inspect its finding. Rows are ranked by severity from the scoring formula." })),
        h("span", { class: "pill warn" }, "Hung = no response in 10 s")),
      matrix(run, selected, select),
      h("p", { class: "meta", text: "Fail = returned an error. J5/J6 inherit Hung when the upload returns no asset to check." }))
      : emptyState({ title: "No experiments in this run", body: run.error || "The run stopped before its first experiment." }),
    evidenceSlot, stripSlot, detailSlot);
  if (selected) showSelected(selected);
  mount(ctx.foot, h("span", { class: "mono", text: run.id }),
    h("span", { text: run.simulated ? "SIMULATED data · not a real finding" : "Saved run results · not live telemetry" }));
}

export default { title: "Run results", icon: "layout-grid", render };
