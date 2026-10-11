// JSON API client. POSTs carry the X-ShutterStorm header the server requires (CSRF guard).
const TIMEOUT_MS = 8000;

async function request(path, opts = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(path, { ...opts, signal: ctl.signal, cache: "no-store" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(body.error || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

export const get = (path) => request(path);
export const post = (path, body = {}) => request(path, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-ShutterStorm": "1" },
  body: JSON.stringify(body),
});

export const api = {
  status: () => get("/api/status"),
  runs: () => get("/api/runs"),
  run: (id) => get(`/api/runs/${encodeURIComponent(id)}`),
  timeline: (id, label) => get(`/api/runs/${encodeURIComponent(id)}/timeline?experiment=${encodeURIComponent(label)}`),
  topology: (id) => get(`/api/runs/${encodeURIComponent(id)}/topology`),
  events: (id, after) => get(`/api/runs/${encodeURIComponent(id)}/events?after=${after | 0}`),
  latestPlan: () => get("/api/plan/latest"),
  safety: () => get("/api/safety"),
  dryPlan: (budget) => post("/api/plan", { budget }),
  planRun: (budget, strict) => post("/api/plan-run", { budget, strict }),
  runFault: (fault, strict) => post("/api/run", { fault, strict }),
  stop: () => post("/api/stop"),
  panic: () => post("/api/panic"),
  reportUrl: (id) => `/runs/${encodeURIComponent(id)}/report.html`,
};
