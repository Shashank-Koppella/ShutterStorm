// History: score trend across runs, every run, and before/after comparisons (never invented).
import { h, icon, mount } from "../js/dom.js";
import { trendChart } from "../js/charts.js";
import { SOURCE, runDate, num, plural, faultTitle } from "../js/format.js";
import { viewHead, emptyState } from "../js/app.js";

function comparisonPanel(run) {
  const c = run?.comparison;
  if (!c || !(c.rows || []).length) {
    return h("div", { class: "panel" }, h("h2", "Before / after"),
      h("p", { class: "muted", text: "No comparison for this run. ShutterStorm never applies a fix: a human applies the suggested fix, then reruns the same faults with --compare-to <earlier run> (or `compare --before A --after B`). Improvement figures appear only from those measured runs." }));
  }
  const maxAbs = Math.max(1, ...c.rows.map((r) => Math.abs(r.delta)));
  return h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", h("h2", "Before / after"), h("div", { class: "meta", text: `Compared with ${c.before_run}. ${c.rule}` })),
      h("span", { class: "pill", text: `Overall ${num(c.overall_before)} → ${num(c.overall_after)}` })),
    h("div", { class: "table-wrap" }, h("table", { class: "t" },
      h("thead", h("tr", ["Fault", "Before", "After", "Change", "Broken before", "Broken after", "Verdict"].map((x) => h("th", { text: x })))),
      h("tbody", c.rows.map((r) => h("tr",
        h("td", h("div", { text: faultTitle(r.fault) }), h("div", { class: "mono meta", text: r.fault })),
        h("td", { class: "n", text: num(r.score_before) }), h("td", { class: "n", text: num(r.score_after) }),
        h("td", h("div", { class: "bar", style: { gridTemplateColumns: "1fr 52px" } },
          h("span", { class: "track" }, h("span", { class: ["fill", r.delta < 0 ? "danger" : ""], style: { width: `${(Math.abs(r.delta) / maxAbs) * 100}%` } })),
          h("span", { class: "val", text: `${r.delta > 0 ? "+" : ""}${num(r.delta)}` }))),
        h("td", { class: "n", text: r.broken_before.join(", ") || "none" }), h("td", { class: "n", text: r.broken_after.join(", ") || "none" }),
        h("td", h("span", { class: `pill ${r.verdict === "improved" ? "ok" : r.verdict === "regressed" ? "danger" : ""}`, text: r.verdict }))))))));
}

async function render(ctx, alive) {
  const runs = ctx.state.runs;
  if (!runs.length) return mount(ctx.el, viewHead({ title: "History" }), emptyState({ body: "Runs appear here after the first run." }));
  const selectedId = ctx.currentRunId();
  const chrono = [...runs].filter((r) => r.overall_score !== null && r.overall_score !== undefined).reverse();
  const run = await ctx.getRun(selectedId).catch(() => null);
  if (!alive()) return;
  mount(ctx.el,
    viewHead({ title: "History", lead: "Every run of this mode, its resilience score over time, and before/after comparisons after a human applied a fix." }),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Resilience score per run"), h("div", { class: "meta", text: `${plural(chrono.length, "scored run")}. Scores depend on which faults each run chose, so compare like with like.` }))),
      trendChart(chrono.map((r) => ({ id: r.id, label: r.id.slice(6, 8) + "/" + r.id.slice(4, 6) + " " + r.id.slice(9, 11) + ":" + r.id.slice(11, 13), value: r.overall_score, selected: r.id === selectedId })),
        { onSelect: (id) => ctx.navigate("results", { run: id }) })),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h2", "All runs")),
      h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", h("tr", ["When", "Run", "Status", "Score", "Experiments", "Auto-stops", "Cleanup", "Planned by", ""].map((x) => h("th", { text: x })))),
        h("tbody", runs.map((r) => h("tr", { class: r.id === selectedId ? "sel" : "" },
          h("td", { class: "n", text: runDate(r.id) }), h("td", { class: "n", text: r.id }),
          h("td", h("span", { class: `pill ${r.status === "completed" ? "ok" : "warn"}`, text: r.status })),
          h("td", { class: "n", text: r.overall_score === null || r.overall_score === undefined ? "—" : num(r.overall_score) }),
          h("td", { class: "n", text: String(r.experiments) }), h("td", { class: "n", text: String(r.auto_stops) }),
          h("td", r.cleanup_ok === null ? h("span", { class: "pill warn", text: "not available" }) : h("span", { class: `pill ${r.cleanup_ok ? "ok" : "danger"}`, text: r.cleanup_ok ? "passed" : "failed" })),
          h("td", { text: `${SOURCE[r.plan_source] || r.plan_source || "—"}${r.ai_decisions ? ` · ${r.ai_decisions} AI decisions` : ""}` }),
          h("td", h("div", { class: "row" },
            h("a", { href: `#/results?run=${encodeURIComponent(r.id)}`, text: "Results" }),
            r.has_report ? h("a", { href: ctx.api.reportUrl(r.id), target: "_blank", rel: "noopener", text: "Report" }) : null)))))))),
    comparisonPanel(run));
  mount(ctx.foot, h("span", { text: ctx.state.mode === "simulated" ? "SIMULATED runs only" : "Real runs only; simulated runs are kept separate" }), h("span", { class: "mono", text: `${runs.length} runs` }));
}

export default { title: "History", icon: "history", render };
