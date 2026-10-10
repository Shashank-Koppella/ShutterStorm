// Shared vocabulary and formatting. Outcome words always accompany colour (never colour alone).
export const JOURNEY_IDS = ["J0", "J1", "J2", "J3", "J4", "J5", "J6"];
export const JOURNEY_SHORT = { J0: "Ping", J1: "Login", J2: "Browse", J3: "Search", J4: "Upload", J5: "Thumb", J6: "Integrity" };
export const JOURNEY_LONG = {
  J0: "Liveness (ping)", J1: "Login", J2: "Browse (metadata search)", J3: "Smart search (ML)",
  J4: "Upload", J5: "Thumbnail generated", J6: "Integrity (SHA-1 of original)",
};
export const OUTCOME = { ok: "OK", slow: "Slow", "fast-fail": "Fail", hung: "Hung", "n/a": "n/a" };
export const OUTCOME_LONG = {
  ok: "OK", slow: "Slow (p95 above 2x baseline and +500 ms)", "fast-fail": "Fail (returned an error)",
  hung: "Hung (no response within the probe timeout)", "n/a": "Not measured",
};
export const SERVICE = { server: "Immich server", database: "PostgreSQL", redis: "Redis", ml: "Machine learning", user: "Browser / API" };
export const FAULT_TITLE = {
  ml_pause: "Freeze machine learning", ml_stop: "Stop machine learning", redis_pause: "Freeze Redis",
  redis_stop: "Stop Redis", db_pause: "Freeze the database", db_stop: "Stop the database",
  db_cpu_throttle: "Starve the database of CPU", server_restart: "Restart the server",
  ml_cpu_throttle: "Starve machine learning of CPU", traffic_spike: "Traffic spike",
  ml_latency: "Slow ML network", ml_packet_loss: "Lossy ML network",
  spike_ml_down: "Traffic spike while ML is down", spike_redis_hung: "Traffic spike while Redis is frozen",
};
export const SOURCE = { live: "AI · live", cache: "AI · cached", fallback: "Template", operator: "Operator", default: "Default" };

export const outcomeClass = (o) => (o === "n/a" || !o ? "na" : o);
export const faultTitle = (id) => FAULT_TITLE[id] || id;

export function num(v, digits = 1) {
  return v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(digits);
}
export function pct(v, digits = 0) {
  return v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(digits)}%`;
}
export function ms(v) {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(v >= 9950 ? 0 : 1)} s` : `${Math.round(v)} ms`;
}
export function secs(v) {
  return v === null || v === undefined ? "—" : `${Number(v).toFixed(v < 10 ? 1 : 0)} s`;
}
export function pad2(n) { return String(n).padStart(2, "0"); }
export function date(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
export function clock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
}
export function ago(ts, now = Date.now() / 1000) {
  if (!ts) return "—";
  const d = Math.max(0, now - ts);
  if (d < 60) return `${Math.round(d)} s ago`;
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}
export function runDate(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id || "");
  if (!m) return id || "—";
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
