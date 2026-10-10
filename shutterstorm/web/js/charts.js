// Hand-drawn SVG/HTML charts in Stormglass tokens. Every chart draws only measured data that the
// API returned; nothing is interpolated or invented. Colours are paired with words or positions.
import { h, s } from "./dom.js";
import { JOURNEY_IDS, JOURNEY_SHORT, OUTCOME, ms } from "./format.js";

function niceStep(span) {
  const steps = [1, 2, 5, 10, 15, 20, 30, 60, 120];
  return steps.find((x) => span / x <= 8) || 300;
}

/**
 * Per-journey latency strips for one experiment: one row per journey, log-scale latency
 * (1 ms .. probe timeout), dots by outcome, the fault window shaded, markers for injection,
 * automatic rollback and undo. `data` is the /timeline response (or a live equivalent).
 */
export function latencyStrips(data, { title = "Latency by journey", live = false } = {}) {
  const W = 920, left = 112, right = 16, top = 30, rowH = 44, gap = 6;
  const H = top + JOURNEY_IDS.length * (rowH + gap) + 24;
  const pts = JOURNEY_IDS.flatMap((j) => data.series[j] || []);
  const timeout = data.timeout_ms || 10000;
  const tmax = Math.max(data.undo_t || 0, data.window_s || 0, ...pts.map((p) => p.t), 5) + 1;
  const tmin = Math.min(-1, ...pts.map((p) => p.t));
  const X = (t) => left + ((t - tmin) / (tmax - tmin)) * (W - left - right);
  const logMax = Math.log10(timeout);
  const Y = (rowTop, lat) => rowTop + rowH - 4 - (Math.log10(Math.max(1, Math.min(lat, timeout))) / logMax) * (rowH - 8);

  const kids = [s("title", { text: `${title}. Fault window shaded; dots coloured and labelled by outcome.` })];
  const bottom = top + JOURNEY_IDS.length * (rowH + gap) - gap;
  if (data.undo_t !== null && data.undo_t !== undefined) {
    kids.push(s("rect", { class: "shade", x: X(0), y: top - 6, width: Math.max(1, X(data.undo_t) - X(0)), height: bottom - top + 12 }));
  } else if (live) {
    kids.push(s("rect", { class: "shade", x: X(0), y: top - 6, width: Math.max(1, X(tmax) - X(0)), height: bottom - top + 12 }));
  }
  const step = niceStep(tmax - tmin);
  for (let t = Math.ceil(tmin / step) * step; t <= tmax; t += step) {
    kids.push(s("line", { class: "grid", x1: X(t), x2: X(t), y1: top - 6, y2: bottom + 6 }));
    kids.push(s("text", { x: X(t), y: bottom + 20, "text-anchor": "middle", text: `${t} s` }));
  }
  const marker = (t, label, cls, anchor = "start") => {
    kids.push(s("line", { class: `marker ${cls || ""}`, x1: X(t), x2: X(t), y1: top - 10, y2: bottom + 6 }));
    kids.push(s("text", { class: "lbl", x: X(t) + (anchor === "start" ? 4 : -4), y: top - 14, "text-anchor": anchor, text: label }));
  };
  // Older runs have no recorded injection time: their axis starts at the first fault probe, while the
  // auto-rollback time is measured from the injection, so that marker would land on the wrong spot.
  const exactAxis = !data.t0_source || data.t0_source === "injection";
  marker(0, exactAxis ? "Injected" : "First fault probe");
  if (data.auto_stop_after_s && exactAxis) marker(data.auto_stop_after_s, `Auto-rollback ${data.auto_stop_after_s} s`, "danger", "end");
  if (data.undo_t !== null && data.undo_t !== undefined && !data.auto_stop_after_s) marker(data.undo_t, data.undo_exact === false ? "Last fault probe" : "Undone", "", "end");

  JOURNEY_IDS.forEach((j, i) => {
    const rowTop = top + i * (rowH + gap);
    const series = data.series[j] || [];
    kids.push(s("line", { class: "axis", x1: left, x2: W - right, y1: rowTop + rowH, y2: rowTop + rowH }));
    kids.push(s("text", { class: "lbl", x: 0, y: rowTop + rowH / 2 + 4, text: `${j} ${JOURNEY_SHORT[j]}` }));
    if (series.length > 1) {
      const d = series.map((p, k) => `${k ? "L" : "M"}${X(p.t).toFixed(1)} ${Y(rowTop, p.latency_ms).toFixed(1)}`).join(" ");
      kids.push(s("path", { class: "line", d }));
    }
    for (const p of series) {
      const c = s("circle", { class: `pt ${p.outcome} ${p.phase === "recovery" ? "recovery" : ""}`, cx: X(p.t), cy: Y(rowTop, p.latency_ms), r: p.outcome === "ok" ? 3 : 4 });
      c.append(s("title", { text: `${j} ${JOURNEY_SHORT[j]} at ${p.t} s: ${OUTCOME[p.outcome] || p.outcome}, ${ms(p.latency_ms)}${p.phase === "recovery" ? " (recovery)" : ""}` }));
      kids.push(c);
    }
    if (!series.length) kids.push(s("text", { x: W - right, y: rowTop + rowH / 2 + 4, "text-anchor": "end", text: "no calls measured (depends on a returned upload)" }));
  });
  return h("div", { class: "chart-scroll" }, s("svg", { class: "chart chart-strips", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title }, kids));
}

export function stripsLegend() {
  return h("div", { class: "legend" },
    h("span", h("i", { class: "ok" }), "OK"), h("span", h("i", { class: "slow" }), "Slow"),
    h("span", h("i", { class: "fast-fail" }), "Fail (error)"), h("span", h("i", { class: "hung" }), "Hung (timeout)"),
    h("span", h("i", { class: "shade" }), "Fault active"), h("span", "Rows: latency on a log scale, 1 ms to the probe timeout"));
}

/** Horizontal bars: [{name, value, max, display, cls, title}] */
export function barList(items, { label = "" } = {}) {
  return h("div", { class: "bars", role: "list", "aria-label": label },
    items.map((it) => h("div", { class: "bar", role: "listitem", title: it.title || "" },
      h("span", { class: "name", text: it.name }),
      h("span", { class: "track" }, h("span", { class: ["fill", it.cls], style: { width: `${Math.max(0, Math.min(1, (it.value || 0) / (it.max || 1))) * 100}%` } })),
      h("span", { class: "val", text: it.display ?? String(it.value) }))));
}

/** A value against a limit (live safety): fill turns red past the limit. */
export function meter({ label, value, limit, max, fmt = (v) => String(v), note = "" }) {
  const v = value ?? 0;
  const over = value !== null && value !== undefined && value > limit;
  const scale = Math.max(max || limit * 1.25, v);
  return h("div", { class: "meter" },
    h("div", { class: "row" }, h("span", label), h("span", { class: "num" }, value === null || value === undefined ? "—" : fmt(value), h("span", { class: "muted" }, ` / limit ${fmt(limit)}`))),
    h("div", { class: "track", role: "meter", "aria-valuenow": v, "aria-valuemax": scale, "aria-label": `${label}: ${fmt(v)} against a limit of ${fmt(limit)}` },
      h("span", { class: ["fill", over ? "danger" : v > limit * 0.8 ? "warn" : ""], style: { width: `${Math.min(1, v / scale) * 100}%` } }),
      h("span", { class: "limit", style: { left: `${Math.min(1, limit / scale) * 100}%` } })),
    note ? h("div", { class: "meta", text: note }) : null);
}

/** Overall score over runs: [{label, value, id, selected}] */
export function trendChart(points, { onSelect } = {}) {
  const W = 920, H = 220, left = 40, right = 16, top = 20, bottom = 40;
  if (!points.length) return h("p", { class: "muted", text: "No scored runs yet." });
  const X = (i) => left + (points.length === 1 ? (W - left - right) / 2 : (i / (points.length - 1)) * (W - left - right));
  const Y = (v) => top + (1 - v / 100) * (H - top - bottom);
  const kids = [s("title", { text: "Overall resilience score per run (0 to 100)" })];
  for (const v of [0, 25, 50, 75, 100]) {
    kids.push(s("line", { class: "grid", x1: left, x2: W - right, y1: Y(v), y2: Y(v) }));
    kids.push(s("text", { x: left - 8, y: Y(v) + 4, "text-anchor": "end", text: String(v) }));
  }
  kids.push(s("path", { class: "line accent", d: points.map((p, i) => `${i ? "L" : "M"}${X(i)} ${Y(p.value)}`).join(" ") }));
  const every = Math.max(1, Math.ceil((points.length * 96) / (W - left - right)));
  points.forEach((p, i) => {
    const g = s("g", { tabindex: onSelect ? 0 : null, role: onSelect ? "button" : null, "aria-label": `${p.label}: ${p.value}` });
    g.append(s("circle", { class: "dot", cx: X(i), cy: Y(p.value), r: p.selected ? 6 : 4 }));
    g.append(s("text", { class: "lbl", x: X(i), y: Y(p.value) - 10, "text-anchor": "middle", text: p.value.toFixed(1) }));
    if (i % every === 0 || i === points.length - 1 || p.selected) g.append(s("text", { x: X(i), y: H - 14, "text-anchor": "middle", text: p.label }));
    if (onSelect) {
      g.addEventListener("click", () => onSelect(p.id));
      g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(p.id); } });
    }
    kids.push(g);
  });
  return s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Overall resilience score per run" }, kids);
}

/**
 * Safety checks per probe round against the rollback limit. `metric` is "error_rate" (0..1)
 * or "p95_ms"; bars that breached a limit are drawn in the danger colour.
 */
export function safetyRounds(rounds, { metric = "error_rate", limit = null } = {}) {
  const W = 640, H = 170, left = 56, right = 12, top = 16, bottom = 28;
  if (!rounds || !rounds.length) return h("p", { class: "meta", text: "No safety rounds recorded for this experiment." });
  const isRate = metric === "error_rate";
  const vals = rounds.map((r) => (r[metric] === null || r[metric] === undefined ? 0 : r[metric]));
  const vmax = isRate ? 1 : Math.max(limit || 0, ...vals) * 1.15 || 1;
  const fmt = (v) => (isRate ? `${Math.round(v * 100)}%` : ms(v));
  const n = rounds.length;
  const slot = (W - left - right) / Math.max(n, 6);
  const bw = Math.min(36, slot - 8);
  const X = (i) => left + i * slot + (slot - bw) / 2;
  const Y = (v) => top + (1 - v / vmax) * (H - top - bottom);
  const kids = [s("title", { text: `${isRate ? "Error rate" : "p95 latency"} per probe round against the rollback limit` })];
  for (const v of [0, vmax / 2, vmax]) {
    kids.push(s("line", { class: "grid", x1: left, x2: W - right, y1: Y(v), y2: Y(v) }));
    kids.push(s("text", { x: left - 8, y: Y(v) + 4, "text-anchor": "end", text: fmt(v) }));
  }
  rounds.forEach((r, i) => {
    const v = vals[i];
    const breached = (r.breached || []).length > 0;
    const rect = s("rect", { class: breached ? "bar-danger" : "bar-muted", x: X(i), y: Y(v), width: bw, height: Math.max(1, Y(0) - Y(v)), rx: 3 });
    rect.append(s("title", { text: `Round ${i + 1}: ${fmt(v)}${breached ? ` (breach: ${r.breached.join("; ")})` : ""}` }));
    kids.push(rect);
    kids.push(s("text", { class: "lbl", x: X(i) + bw / 2, y: Y(v) - 6, "text-anchor": "middle", text: fmt(v) }));
    kids.push(s("text", { x: X(i) + bw / 2, y: H - 8, "text-anchor": "middle", text: `round ${i + 1}` }));
  });
  if (limit !== null && limit !== undefined) {
    kids.push(s("line", { class: "limitline", x1: left, x2: W - right, y1: Y(limit), y2: Y(limit) }));
    kids.push(s("text", { x: W - right, y: Y(limit) - 6, "text-anchor": "end", text: `limit ${fmt(limit)}` }));
  }
  return s("svg", { class: "chart chart-small", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${isRate ? "Error rate" : "p95 latency"} per probe round` }, kids);
}
