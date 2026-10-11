// AI decisions: what the planner chose before each experiment, what the controller clamped, how it went.
import { h, icon, mount } from "../js/dom.js";
import { barList } from "../js/charts.js";
import { JOURNEY_IDS, JOURNEY_SHORT, SOURCE, faultTitle, pct, num, plural } from "../js/format.js";
import { runPicker, viewHead, emptyState } from "../js/app.js";

function metric(value, caption, cls = "") {
  return h("div", { class: "metric" }, h("div", { class: `value ${cls}`, text: value }), h("div", { class: "caption", text: caption }));
}

function decisionCard(d, exp, caps) {
  const strength = Object.entries(d.settings || {}).filter(([k]) => !k.startsWith("max_") && k !== "duration_s");
  const ai = d.source === "live" || d.source === "cache";
  const set = d.settings || {};
  return h("li",
    h("span", { class: "n", text: String(d.step).padStart(2, "0") }),
    h("div", { class: "stack" },
      h("div", { class: "row between" },
        h("div", h("span", { class: "title", text: faultTitle(d.fault) }), h("span", { class: "mono meta", text: `  ${d.fault}` })),
        h("div", { class: "row" },
          h("span", { class: `pill ${ai ? "ai" : ""}` }, ai ? icon("sparkles") : null, SOURCE[d.source] || d.source),
          h("span", { class: `pill ${d.followed_plan ? "" : "warn"}`, text: d.followed_plan ? "As planned" : `Changed plan · was ${d.plan_next}` }))),
      h("p", { class: "ai-text", style: { margin: "0" }, text: d.reason }),
      h("div", { class: "grid3" },
        h("div", h("div", { class: "meta", text: "How hard, how long" }), h("div", { class: "num", text: [...strength.map(([k, v]) => `${k} ${v}`), `${num(set.duration_s, 0)} s`].join(" · ") })),
        h("div", h("div", { class: "meta", text: "Safety limits (AI · cap)" }),
          h("div", { class: "num", text: `errors ${pct(set.max_error_rate)} · ${pct(caps?.max_error_rate)}` }),
          h("div", { class: "num", text: `p95 ${num(set.max_p95_ms, 0)} ms · ${num(caps?.max_p95_ms, 0)} ms` })),
        h("div", h("div", { class: "meta", text: "Outcome (measured)" }),
          exp ? h("div", { class: "row" }, h("span", { class: "num", text: `score ${num(exp.scoring?.score)}` }),
            exp.auto_stopped ? h("span", { class: "pill warn", text: `Auto-stopped at ${exp.auto_stopped.after_s} s` }) : h("span", { class: "pill", text: "Full window" }))
            : h("span", { class: "meta", text: "not run" }))),
      h("p", { class: "meta", style: { margin: "0" }, text: `Why these limits: ${d.safety_reason}` }),
      (d.clamped || []).length ? h("div", { class: "notice", style: { marginTop: "4px" } }, icon("shield-check"),
        h("div", h("b", "Controller clamped: "), d.clamped.join("; "))) : null));
}

function predictionRows(run) {
  return h("div", { class: "matrix" },
    h("div", { class: "mrow head" }, h("span", { class: "meta", text: "Experiment" }), JOURNEY_IDS.map((j) => h("span", { class: "mcol" }, h("b", j), JOURNEY_SHORT[j])), h("span")),
    run.experiments.map((e) => {
      const pred = new Set(e.prediction?.predicted_impacted || []), act = new Set(e.actual_impacted || []);
      return h("div", { class: "mrow" }, h("span", { class: "mono", style: { fontSize: "12px" }, text: e.label }),
        JOURNEY_IDS.map((j) => {
          const p = pred.has(j), a = act.has(j);
          const [cls, word] = p && a ? ["hung", "Hit"] : a ? ["fast-fail", "Missed"] : p ? ["slow", "Over"] : ["na", "—"];
          return h("span", { class: `cell ${cls}`, title: `${e.label} ${j}: ${p ? "predicted" : "not predicted"}, ${a ? "broke" : "stayed OK"}` }, h("span", { class: "jl", text: `${j} ${JOURNEY_SHORT[j]}` }), word);
        }), h("span"));
    }));
}

async function render(ctx, alive) {
  const runId = ctx.currentRunId();
  if (!runId) return mount(ctx.el, viewHead({ title: "AI decisions" }), emptyState({ body: "Decisions appear after the first run." }));
  const run = await ctx.getRun(runId);
  if (!alive()) return;
  const decs = run.decisions || [];
  const caps = (ctx.state.status?.catalog || {}).caps;
  const ai = decs.filter((d) => d.source === "live" || d.source === "cache").length;
  const changed = decs.filter((d) => d.followed_plan === false).length;
  const clamps = decs.reduce((n, d) => n + (d.clamped || []).length, 0);
  const stats = run.llm_stats || {};
  const sc = run.scorecard || {};
  const byFault = Object.fromEntries(run.experiments.map((e) => [e.fault, e]));
  const plan = run.plan || {};

  mount(ctx.el,
    viewHead({ title: "AI decisions", lead: "What the planner chose before each experiment, how hard and how cautiously, what the controller clamped, and what was measured.", right: runPicker(run.id) }),
    h("div", { class: "metrics" },
      metric(decs.length ? `${ai}/${decs.length}` : "—", "Decisions made by the AI"),
      metric(String(changed).padStart(2, "0"), "Times it changed its own plan", changed ? "warn" : ""),
      metric(String(clamps).padStart(2, "0"), "Values clamped by the controller"),
      metric(sc.precision !== undefined ? `${num(sc.precision, 2)} / ${num(sc.recall, 2)}` : "—", "Prediction precision / recall")),
    decs.length ? h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Decision by decision"), h("div", { class: "meta", text: "AI text is the planner's own reasoning (unverified); limits and outcomes are enforced and measured by code." }))),
      h("ol", { class: "steps" }, decs.map((d) => decisionCard(d, byFault[d.fault], caps))))
      : h("div", { class: "panel" }, h("div", { class: "notice" }, icon("info"),
        h("div", { text: plan.source === "operator" ? "This run's experiments were chosen by the operator, so there are no AI decisions." : "This run was made before per-step AI decisions were recorded; its tentative plan is shown below." }))),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", h("h2", "Tentative plan vs what ran"), h("div", { class: "meta", text: plan.strategy || "" }))),
        h("div", { class: "grid2" },
          h("div", h("h3", "Planned at the start"), h("ol", { class: "meta" }, (plan.items || []).map((i) => h("li", { class: "mono", text: i.fault })))),
          h("div", h("h3", "Actually ran"), h("ol", { class: "meta" }, (decs.length ? decs.map((d) => d.fault) : run.experiments.map((e) => e.fault)).map((f) => h("li", { class: "mono", text: f })))))),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", h("h2", "Where the AI text came from"), h("div", { class: "meta", text: stats.agents ? `Agents: ${stats.agents}` : "" }))),
        barList(["live", "cache", "fallback"].map((k) => ({ name: SOURCE[k], value: stats[k] || 0, max: Math.max(1, (stats.live || 0) + (stats.cache || 0) + (stats.fallback || 0)), display: String(stats[k] ?? "—"), cls: k === "fallback" ? "muted" : "" })), { label: "AI answer sources" }),
        h("p", { class: "meta", text: `${plural(stats.api_calls || 0, "API call")} including retries. Fallback = a deterministic template replaced an invalid or unavailable AI answer.` }))),
    run.experiments.length ? h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Predicted vs measured impact"), h("div", { class: "meta", text: "Hit = predicted and broke · Missed = broke but not predicted · Over = predicted but stayed OK" }))),
      predictionRows(run)) : null);
  mount(ctx.foot, h("span", { class: "mono", text: run.id }), h("span", { text: run.simulated ? "SIMULATED data" : "Saved run data" }));
}

export default { title: "AI decisions", icon: "sparkles", render };
