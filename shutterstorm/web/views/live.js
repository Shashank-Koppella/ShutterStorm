// Live run: built only from structured events (runs/<id>/events.jsonl). Follows the running job;
// when idle, shows a recorded run and can replay its events at speed (no faults injected).
import { h, icon, mount } from "../js/dom.js";
import { latencyStrips, stripsLegend, meter } from "../js/charts.js";
import { JOURNEY_IDS, JOURNEY_SHORT, JOURNEY_LONG, OUTCOME, SOURCE, faultTitle, ms, pct, num, clock, secs } from "../js/format.js";
import { viewHead, emptyState, setParams } from "../js/app.js";

let S = null; // view state: { runId, live, seq, events, slots, replay }

function derive(events) {
  const d = { step: null, plan: null, experiments: [], decisions: {}, current: null, run: null, finished: null, cleanup: null, baseline: [] };
  const byFault = {};
  for (const ev of events) {
    switch (ev.type) {
      case "run_started": d.run = ev; break;
      case "step": d.step = ev.name; break;
      case "plan": d.plan = ev; break;
      case "baseline_round": d.baseline.push(ev); break;
      case "decision": d.decisions[ev.fault] = ev; break;
      case "experiment_started": {
        const x = { fault: ev.fault, started: ev, rounds: [], safety: null, rollback: null, undone: null, recovered: null, done: null, injected: null };
        byFault[ev.fault] = x; d.experiments.push(x); d.current = x; break;
      }
      case "injected": {
        let x = byFault[ev.fault];
        if (!x) { x = { fault: ev.fault, started: null, rounds: [], safety: null, rollback: null, undone: null, recovered: null, done: null }; byFault[ev.fault] = x; d.experiments.push(x); d.current = x; }
        x.injected = ev; break;
      }
      case "probe_round": byFault[ev.fault]?.rounds.push(ev); break;
      case "safety_check": if (byFault[ev.fault]) byFault[ev.fault].safety = ev; break;
      case "rollback": if (byFault[ev.fault]) byFault[ev.fault].rollback = ev; break;
      case "undone": if (byFault[ev.fault]) byFault[ev.fault].undone = ev; break;
      case "recovered": if (byFault[ev.fault]) byFault[ev.fault].recovered = ev; break;
      case "experiment_done": if (byFault[ev.fault]) byFault[ev.fault].done = ev; break;
      case "cleanup": d.cleanup = ev; break;
      case "run_finished": d.finished = ev; break;
      default: break;
    }
  }
  return d;
}

function seriesOf(x) {
  const series = Object.fromEntries(JOURNEY_IDS.map((j) => [j, []]));
  for (const r of x.rounds) for (const p of r.results) series[p.journey]?.push({ t: r.t_rel, latency_ms: p.latency_ms, outcome: p.outcome, phase: r.phase });
  return {
    series, window_s: x.injected?.window_s ?? x.started?.duration_s, timeout_ms: 10000,
    undo_t: x.undone ? x.undone.t_rel : null, undo_exact: true, auto_stop_after_s: x.rollback?.after_s ?? null,
  };
}

function tiles(x) {
  const last = x ? [...x.rounds].reverse().find((r) => r.phase === "fault") || x.rounds[x.rounds.length - 1] : null;
  const res = Object.fromEntries((last?.results || []).map((r) => [r.journey, r]));
  return h("div", { class: "tiles", role: "list", "aria-label": "Latest probe round" }, JOURNEY_IDS.map((j) => {
    const r = res[j];
    return h("div", { class: `tile ${r ? r.outcome : ""}`, role: "listitem", title: JOURNEY_LONG[j] },
      h("div", { class: "j", text: `${j} · ${JOURNEY_SHORT[j]}` }),
      h("div", { class: "o", text: r ? OUTCOME[r.outcome] : "—" }),
      h("div", { class: "l", text: r ? ms(r.latency_ms) : (last ? "not in this round" : "waiting") }));
  }));
}

function decisionCard(dec, x) {
  if (!dec) return h("p", { class: "meta", text: x ? "Chosen by the operator; default strength and limits." : "Waiting for the first decision." });
  const strength = Object.entries(dec.settings || {}).filter(([k]) => !k.startsWith("max_")).map(([k, v]) => (k === "duration_s" ? `${v} s` : `${k} = ${v}`)).join(" · ");
  return h("div", { class: "stack" },
    h("div", { class: "row" }, h("span", { class: `pill ${dec.source === "live" || dec.source === "cache" ? "ai" : ""}` }, dec.source === "live" || dec.source === "cache" ? icon("sparkles") : null, SOURCE[dec.source] || dec.source),
      h("span", { class: `pill ${dec.followed_plan ? "" : "warn"}`, text: dec.followed_plan ? "As planned" : `Changed plan (was ${dec.plan_next})` })),
    h("div", h("b", { text: faultTitle(dec.fault) }), h("span", { class: "mono meta", text: `  ${dec.fault}` })),
    h("p", { class: "ai-text", text: dec.reason }),
    h("dl", { class: "kv" }, h("dt", "Strength"), h("dd", strength || "—"),
      h("dt", "Limits"), h("dd", dec.safety || "—")),
    h("p", { class: "meta", text: dec.safety_reason }),
    (dec.clamped || []).length ? h("p", { class: "meta", text: `Controller clamped: ${dec.clamped.join("; ")}` }) : null);
}

function stepList(d) {
  const planned = d.plan?.items || [];
  const ran = d.experiments.map((x) => x.fault);
  return h("ol", { class: "steps" }, d.experiments.map((x, i) => {
    const done = x.done;
    const state = done ? (x.rollback ? "Auto-stopped" : "Done") : x.undone ? "Recovering" : x.injected ? "Fault active" : "Starting";
    return h("li", h("span", { class: "n", text: String(i + 1).padStart(2, "0") }),
      h("div", h("div", { class: "title", text: faultTitle(x.fault) }),
        h("small", { text: `${x.fault} · ${state}${done ? ` · score ${num(done.score)} (${done.severity_label})` : ""}` })));
  }), planned.filter((p) => !ran.includes(p.fault)).map((p) => h("li", { class: "muted" },
    h("span", { class: "n", text: "··" }), h("div", h("div", { text: faultTitle(p.fault) }), h("small", { text: `${p.fault} · tentative plan; the AI re-decides before each step` })))));
}

function logLines(events) {
  const fmt = (e) => {
    switch (e.type) {
      case "probe_round": return `${e.fault} ${e.phase} #${e.round}: ${e.results.map((r) => `${r.journey}=${OUTCOME[r.outcome]}`).join(" ")}`;
      case "safety_check": return `${e.fault} round ${e.round}: errors ${pct(e.error_rate)}, p95 ${e.p95_ms === null ? "—" : ms(e.p95_ms)}${e.breached.length ? ` · BREACH: ${e.breached.join("; ")}` : ""}`;
      case "decision": return `${e.fault} (${SOURCE[e.source] || e.source}) ${e.reason}`;
      case "step": return e.name;
      case "rollback": return `${e.fault} rolled back after ${e.after_s} s: ${e.reason}`;
      case "undone": return `${e.fault} undo ${e.ok ? "verified" : "NOT verified"}: ${e.detail}`;
      case "experiment_done": return `${e.fault} score ${num(e.score)} (${e.severity_label})`;
      case "cleanup": return `cleanup ${e.ok ? "PASS" : "FAIL"} (${e.checks} checks)${e.failed.length ? `: ${e.failed.join(", ")}` : ""}`;
      case "run_finished": return `run ${e.status}, overall ${e.overall_score ?? "—"}`;
      default: return Object.entries(e).filter(([k]) => !["seq", "t", "type"].includes(k)).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ").slice(0, 200);
    }
  };
  return events.slice(-80).reverse().map((e) => h("div", h("span", { class: "t", text: clock(e.t) }), h("span", { class: "ty", text: e.type }), h("span", { text: fmt(e) })));
}

function paint() {
  if (!S) return;
  const events = S.replay ? S.events.slice(0, S.replay.cursor) : S.events;
  const d = derive(events);
  const x = S.pick ? d.experiments.find((e) => e.fault === S.pick) || d.current : d.current;
  const sl = S.slots;
  const phase = d.finished ? `Run ${d.finished.status}` : d.step || "Waiting for events";
  mount(sl.phase, h("span", { class: `tag ${d.finished ? "" : "warn"}`, text: S.replay ? `Replay · ${phase}` : S.live ? `Live · ${phase}` : `Recorded · ${phase}` }));
  mount(sl.tiles, tiles(x));
  if (x) {
    mount(sl.strips, latencyStrips(seriesOf(x), { title: `${x.fault}: latency by journey`, live: !x.undone }), stripsLegend());
    mount(sl.head, h("h2", { text: faultTitle(x.fault) }), h("div", { class: "meta", text: `${x.fault}${x.started?.parts?.length > 1 ? ` · ${x.started.parts.join(" + ")}` : ""} · window ${secs(x.injected?.window_s ?? x.started?.duration_s)}` }));
    const sc = x.safety;
    const lim = sc?.limits || x.started?.limits || x.injected?.limits || {};
    mount(sl.safety,
      meter({ label: "Error rate this round", value: sc ? sc.error_rate : null, limit: lim.max_error_rate ?? 0.75, max: 1, fmt: (v) => pct(v) }),
      meter({ label: "p95 latency of successful calls", value: sc ? sc.p95_ms : null, limit: lim.max_p95_ms ?? 8000, fmt: (v) => ms(v) }),
      h("p", { class: "meta", text: sc ? (sc.outside_blast_radius.length ? `Blast radius exceeded: ${sc.outside_blast_radius.join(", ")}` : "Blast radius: no unrelated journey failed") : "No safety check yet." }),
      x.rollback ? h("div", { class: "notice warn" }, icon("undo-2"), h("div", { text: `Automatic rollback after ${x.rollback.after_s} s: ${x.rollback.reason}` })) : null,
      x.undone ? h("p", { class: "meta", text: x.undone.ok ? "Undo verified." : `Undo NOT verified: ${x.undone.detail}` }) : null,
      x.recovered ? h("p", { class: "meta", text: x.recovered.recovery_s === null ? "Did not recover within the limit." : `Recovered ${secs(x.recovered.recovery_s)} after undo.` }) : null);
    mount(sl.decision, decisionCard(d.decisions[x.fault], x));
  } else {
    mount(sl.strips, h("p", { class: "meta", text: "No experiment has started yet." }));
    mount(sl.head, h("h2", "Current experiment"));
    mount(sl.safety, h("p", { class: "meta", text: "Safety meters appear with the first fault." }));
    mount(sl.decision, decisionCard(null, null));
  }
  mount(sl.steps, d.experiments.length || d.plan ? stepList(d) : h("p", { class: "meta", text: "No experiments yet." }));
  mount(sl.picker, d.experiments.length > 1 ? h("label", { class: "runpick" }, h("span", { class: "meta", text: "Experiment" }),
    h("select", { onchange: (e) => { S.pick = e.target.value || null; paint(); } },
      h("option", { value: "", text: "Follow current" }), d.experiments.map((e) => h("option", { value: e.fault, selected: S.pick === e.fault, text: e.fault })))) : null);
  mount(sl.log, logLines(events));
  if (d.cleanup) mount(sl.cleanup, h("span", { class: `pill ${d.cleanup.ok ? "ok" : "danger"}`, text: `Cleanup ${d.cleanup.ok ? "passed" : "FAILED"} · ${d.cleanup.checks} checks` }));
  else mount(sl.cleanup);
}

async function load(ctx) {
  const res = await ctx.api.events(S.runId, S.seq);
  if (res.events.length) {
    S.events.push(...res.events);
    S.seq = res.last;
    if (!S.replay) paint();
  }
}

async function render(ctx, alive) {
  const st = ctx.state.status;
  const job = st?.job || {};
  const liveId = job.running && job.kind === "run" ? job.run_id : null;
  const recorded = ctx.state.runs.filter((r) => r.has_events);
  const runId = liveId || (ctx.params.run && recorded.some((r) => r.id === ctx.params.run) ? ctx.params.run : recorded[0]?.id);
  if (S?.replay?.timer) clearInterval(S.replay.timer);
  if (!runId) {
    S = null;
    return mount(ctx.el, viewHead({ title: "Live run", lead: "Watch a fault happen: journeys, latency and safety limits, round by round." }),
      emptyState({ title: job.running ? "Waiting for the run to start" : "No run in progress",
        body: "Start an AI-planned run from Experiment plan. Live views use structured run events, recorded by runs made with this dashboard version.",
        actions: [h("a", { class: "btn btn-primary", href: "#/plan" }, icon("play"), "Go to Experiment plan")] }));
  }
  S = { runId, live: Boolean(liveId), seq: 0, events: [], pick: null, replay: null, slots: {} };
  const sl = S.slots;
  for (const k of ["phase", "tiles", "strips", "head", "safety", "decision", "steps", "log", "picker", "cleanup"]) sl[k] = h("div");
  sl.phase = h("span");
  sl.log = h("div", { class: "eventlog", role: "log", "aria-label": "Structured run events, newest first" });
  const replayBtn = h("button", { type: "button", hidden: S.live, onclick: () => startReplay() }, icon("play"), "Replay at 8×");
  const picker = !S.live && recorded.length ? h("label", { class: "runpick" }, h("span", { class: "meta", text: "Recorded run" }),
    h("select", { onchange: (e) => setParams({ run: e.target.value }) }, recorded.map((r) => h("option", { value: r.id, selected: r.id === runId, text: r.id })))) : null;
  mount(ctx.el,
    viewHead({ tag: null, title: "Live run", lead: S.live ? "Following the running job. Every number below comes from a structured run event." : "No run in progress. Showing the events recorded for this run; replay them to see it unfold.",
      right: h("div", { class: "row" }, picker, replayBtn) }),
    h("div", { class: "row between", style: { marginTop: "16px" } }, sl.phase, h("div", { class: "row" }, sl.picker, sl.cleanup)),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", h("h2", "User journeys, latest round"), h("div", { class: "meta", text: "Each tile is the most recent measured call of that journey" }))), sl.tiles),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", sl.head)), sl.strips),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", "Safety limits"), h("div", { class: "meta", text: "Checked after every probe round; a breach rolls the fault back at once" }), sl.safety),
      h("div", { class: "panel" }, h("h2", "Decision for this experiment"), sl.decision)),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", "Experiments"), sl.steps),
      h("div", { class: "panel" }, h("h2", "Event log"), sl.log)));
  mount(ctx.foot, h("span", { class: "mono", text: runId }), h("span", { text: S.live ? "Live: structured events, polled every 1.5 s" : "Recorded events · not live telemetry" }));
  await load(ctx);
  if (!alive()) return;
  paint();

  function startReplay() {
    if (!S || !S.events.length) return;
    if (S.replay?.timer) clearInterval(S.replay.timer);
    const t0 = S.events[0].t;
    const start = performance.now();
    S.replay = { cursor: 1, timer: null };
    S.replay.timer = setInterval(() => {
      const simT = t0 + ((performance.now() - start) / 1000) * 8;
      let c = S.replay.cursor;
      while (c < S.events.length && S.events[c].t <= simT) c += 1;
      if (c !== S.replay.cursor) { S.replay.cursor = c; paint(); }
      if (c >= S.events.length) { clearInterval(S.replay.timer); S.replay = null; paint(); }
    }, 120);
  }
}

async function tick(ctx) {
  // A run can start (or report its run id) after this screen was rendered, e.g. when "Start AI run"
  // navigates here before the job's first status poll: switch to following it as soon as it is known.
  const job = ctx.state.status?.job || {};
  const liveId = job.running && job.kind === "run" ? job.run_id : null;
  if (liveId && (!S || !S.live || S.runId !== liveId)) return render(ctx, () => true);
  if (S?.live) await load(ctx);
}

export default { title: "Live run", icon: "activity", render, tick };
