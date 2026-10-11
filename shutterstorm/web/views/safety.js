// Safety record: live safety state, per-experiment limits, threshold rollbacks (breach -> rollback ->
// recovery), and cleanup verification. Missing evidence is shown as "Not available", never as PASS.
import { h, icon, mount } from "../js/dom.js";
import { safetyRounds } from "../js/charts.js";
import { faultTitle, pct, num, secs, ago, clock } from "../js/format.js";
import { runPicker, viewHead, emptyState } from "../js/app.js";

function check(ok, name, detail) {
  return h("div", { class: `check ${ok ? "ok" : "bad"}` }, icon(ok ? "circle-check" : "circle-x"), h("span", { text: name }), h("span", { class: "d", text: detail || "" }));
}

function nowPanel(st, safe) {
  const g = safe?.guard || {};
  const guardText = !g.seen ? (safe?.journal?.length ? "NOT running while faults are active" : "Idle: starts with the next fault")
    : g.mode === "container" ? (g.alive ? `Guard container alive (heartbeat ${g.heartbeat_age_s} s ago)` : `Guard container silent for ${g.heartbeat_age_s} s`)
      : (g.alive ? "Guard process running" : "Guard process not running");
  return h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", h("h2", "Right now"), h("div", { class: "meta", text: "Live safety state of the sandbox, refreshed with the status" }))),
    h("div", { class: "grid2" },
      h("div",
        h("h3", "Containers"),
        (st?.containers || []).map((c) => check(c.exists && c.running && !c.paused && (!c.health || c.health === "healthy"), c.name,
          `${c.exists ? (c.running ? "running" : "stopped") : "missing"} · ${c.paused ? "paused" : "not paused"} · health ${c.health ?? "none"}`)),
        h("p", { class: "meta", text: "Container health alone is not proof of cleanup: Postgres reports health only every 300 s." })),
      h("div",
        h("h3", "Undo journal and guard"),
        safe?.journal?.length ? safe.journal.map((e) => check(false, `Active fault: ${e.label}`, `${e.recipe} · deadline ${clock(e.deadline)}`))
          : check(true, "No active faults", "journal empty"),
        check(!(safe?.journal?.length) || g.alive, "Dead-man guard", guardText),
        check(!safe?.stop_file, "STOP file", safe?.stop_file ? "present: a running workflow stops at its next step" : "not present"),
        h("p", { class: "meta", text: `Caps: ${safe?.caps || "—"}` }))));
}

function rollbackStory(e, rounds, limitRate) {
  const a = e.auto_stopped;
  return h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", h("h2", { text: `${faultTitle(e.fault)} / ${e.label}` }), h("div", { class: "meta", text: "Threshold-triggered rollback" })),
      h("span", { class: "pill warn", text: "Auto-stopped" })),
    h("div", { class: "story" },
      h("div", h("span", { class: "tag warn", text: "01 / Breach detected" }), h("p", { text: a.reason })),
      h("div", h("span", { class: "tag warn", text: "02 / Automatic rollback" }), h("p", { text: `Fault stopped at ${a.after_s} s of the planned ${secs(e.window_s)} window (round ${a.round}).` }),
        h("p", { class: "meta", text: e.undo ? (e.undo.ok ? "Undo verified by reading the container state back." : `Undo NOT verified: ${e.undo.detail}`) : "Undo state not recorded." })),
      h("div", h("span", { class: "tag", text: "03 / Recovery measured" }),
        h("p", { text: e.recovery_s === null || e.recovery_s === undefined ? "Journeys did not recover within the limit." : `User journeys recovered ${secs(e.recovery_s)} after the undo.` }))),
    h("div", { style: { marginTop: "16px" } }, rounds));
}

async function render(ctx, alive) {
  const runId = ctx.currentRunId();
  const safe = await ctx.api.safety().catch(() => null);
  if (!alive()) return;
  if (!runId) return mount(ctx.el, viewHead({ title: "Safety record" }), nowPanel(ctx.state.status, safe), emptyState({ body: "Run records appear after the first run." }));
  const run = await ctx.getRun(runId);
  if (!alive()) return;
  const autos = run.experiments.filter((e) => e.auto_stopped);
  const cl = run.cleanup;
  const hasChecks = cl && cl.checks && cl.checks.length;
  const pingHid = run.experiments.find((e) => e.heatmap?.J0 === "ok" && Object.values(e.heatmap || {}).filter((v) => v === "hung").length >= 3);

  const storySlots = autos.map(() => h("div"));
  mount(ctx.el,
    viewHead({ tag: hasChecks ? (cl.ok ? `Cleanup verified · ${cl.checks.length}/${cl.checks.length} checks` : "Cleanup FAILED") : "Cleanup evidence not available",
      tagClass: hasChecks ? (cl.ok ? "" : "danger") : "warn", title: "Safety record",
      lead: "Rollbacks triggered by limits, the state of the sandbox now, and the evidence that each run cleaned up after itself.", right: runPicker(run.id) }),
    nowPanel(ctx.state.status, safe),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Limits per experiment"), h("div", { class: "meta", text: `Caps for this run: ${run.thresholds || "—"}` }))),
      h("div", { class: "table-wrap" }, h("table", { class: "t" },
        h("thead", h("tr", ["Experiment", "Error limit", "p95 limit", "Window", "Ran for", "Outcome"].map((x) => h("th", { text: x })))),
        h("tbody", run.experiments.map((e) => {
          const s = e.decision?.settings || {};
          return h("tr", h("td", { class: "n", text: e.label }), h("td", { class: "n", text: s.max_error_rate !== undefined ? pct(s.max_error_rate) : "cap" }),
            h("td", { class: "n", text: s.max_p95_ms !== undefined ? `${num(s.max_p95_ms, 0)} ms` : "cap" }), h("td", { class: "n", text: secs(e.window_s) }),
            h("td", { class: "n", text: secs(e.fault_duration_s) }),
            h("td", e.auto_stopped ? h("span", { class: "pill warn", text: "Auto-stopped" }) : h("span", { class: "pill", text: "Full window" })));
        }))))),
    autos.length ? autos.map((e, i) => storySlots[i]) : h("div", { class: "panel" }, h("p", { class: "muted", text: "No experiment in this run crossed a safety limit." })),
    pingHid ? h("div", { class: "panel" }, h("h2", "A healthy ping can hide a frozen app"),
      h("p", { class: "muted", text: `During ${pingHid.label}, ping (J0) stayed OK while other journeys hung. Keep container health and user-journey evidence separate.` })) : null,
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Cleanup verification"), h("div", { class: "meta", text: hasChecks ? `Checked after the run, ${ago(cl.time)}` : (cl?.note || "No cleanup evidence recorded for this run") })),
        hasChecks ? h("span", { class: `pill ${cl.ok ? "ok" : "danger"}`, text: cl.ok ? "PASS" : "FAIL" }) : h("span", { class: "pill warn", text: "Not available" })),
      hasChecks ? cl.checks.map((c) => check(c.ok, c.name, c.detail)) : h("p", { class: "muted", text: "Without recorded checks this run's cleanup is unknown, not passed." })));
  mount(ctx.foot, h("span", { class: "mono", text: run.id }), h("span", { text: "Rollback request ≠ restoration ≠ verified cleanup" }));

  for (const [i, e] of autos.entries()) {
    let rounds = h("p", { class: "meta", text: "Per-round data not available." });
    const latency = /p95/.test(e.auto_stopped.reason || "") && !/error rate/.test(e.auto_stopped.reason || "");
    const set = e.decision?.settings || {};
    const caps = ctx.state.status?.catalog?.caps || {};
    const limit = latency ? (set.max_p95_ms ?? caps.max_p95_ms) : (set.max_error_rate ?? caps.max_error_rate);
    try {
      const t = await ctx.api.timeline(run.id, e.label);
      if (!alive()) return;
      rounds = h("div", h("h3", { text: latency ? "p95 latency per probe round (the breached limit)" : "Error rate per probe round (the breached limit)" }),
        safetyRounds(t.safety_rounds, { metric: latency ? "p95_ms" : "error_rate", limit }));
    } catch { /* keep the placeholder */ }
    mount(storySlots[i], rollbackStory(e, rounds));
  }
}

async function tick(ctx) {
  // the "Right now" panel is cheap to refresh; re-render only it would need slots: keep it simple and refresh every ~6 s
  ctx.state._safetyTick = (ctx.state._safetyTick || 0) + 1;
  if (ctx.state._safetyTick % 4 === 0) {
    const safe = await ctx.api.safety().catch(() => null);
    const panel = ctx.el.querySelector(".panel");
    if (panel && safe) panel.replaceWith(nowPanel(ctx.state.status, safe));
  }
}

export default { title: "Safety record", icon: "shield-check", render, tick };
