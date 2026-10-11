// Dependency graph from the selected run's saved topology.json (not live health). Edges are grouped
// by service pair; source-specific evidence (Compose, runtime config, empirical) is in the panel.
import { h, icon, mount, announce } from "../js/dom.js";
import { JOURNEY_IDS, JOURNEY_SHORT, OUTCOME, SERVICE } from "../js/format.js";
import { runPicker, viewHead, emptyState, appState } from "../js/app.js";

const DEPS = ["database", "redis", "ml"];
let selectedNode = "ml";

function portOf(edges, to) {
  for (const e of edges) {
    if (e.from === "server" && e.to === to) {
      const m = /:(\d{2,5})/.exec(e.label);
      if (m) return m[1];
    }
  }
  return null;
}

function predictedFor(topo, service) {
  return JOURNEY_IDS.filter((j) => (topo.journey_deps[j] || []).includes(service));
}
function observedFor(topo, service) {
  const set = new Set();
  for (const x of topo.impacts[service] || []) for (const j of x.impacted || []) set.add(j);
  return JOURNEY_IDS.filter((j) => set.has(j));
}
const range = (js) => (js.length ? (js.length > 2 ? `${js[0]}–${js[js.length - 1]}` : js.join(", ")) : "none recorded");

function node(topo, key, onSelect) {
  const edges = topo.edges;
  const declared = edges.some((e) => e.source === "compose" && e.from === "server" && e.to === key);
  const config = edges.some((e) => e.source === "config" && e.from === "server" && e.to === key);
  const port = portOf(edges, key);
  const observed = observedFor(topo, key);
  const decl = declared ? (config ? "Compose + config" : "Compose") : config ? "Config only · absent from Compose" : "Not declared";
  return h("button", { type: "button", class: `tnode ${declared ? "" : "undeclared"}`, "aria-pressed": String(selectedNode === key), "data-node": key, onclick: () => onSelect(key) },
    h("strong", { text: SERVICE[key] || key }),
    h("small", { text: `${port ? `:${port} · ` : ""}${decl}` }),
    h("small", { text: `Observed impact: ${range(observed)}` }));
}

function detail(topo, key) {
  const edges = topo.edges;
  const pair = (src) => edges.filter((e) => e.source === src && ((e.from === "server" && e.to === key) || (key === "server" && (e.from === "server" || e.to === "server"))));
  const compose = pair("compose"), config = pair("config"), empirical = pair("empirical");
  const predicted = key === "server" ? JOURNEY_IDS : predictedFor(topo, key);
  const observed = observedFor(topo, key);
  const undeclared = key !== "server" && !compose.length;
  const line = (tag, items, none) => h("div", { class: "evline" }, h("span", { class: "tag", text: tag }),
    items.length ? h("ul", { class: "meta", style: { margin: "4px 0 0", paddingLeft: "18px" } }, items.map((t) => h("li", { text: t }))) : h("div", { class: "meta", text: none }));
  const experiments = topo.impacts[key] || [];
  return h("div", { class: "panel", "aria-live": "polite" },
    h("div", { class: "panel-head" }, h("h2", { text: SERVICE[key] || key }),
      h("span", { class: `pill ${undeclared ? "warn" : ""}`, text: key === "server" ? "Public entry point" : undeclared ? "Undeclared in Compose" : "Declared + observed" })),
    line("Compose evidence", compose.map((e) => `${e.from} → ${e.to}: ${e.label}`), key === "server" ? "—" : `No server → ${key} depends_on entry in Compose.`),
    line("Runtime configuration", config.map((e) => `${e.from} → ${e.to}: ${e.label}`), "No runtime configuration evidence recorded."),
    line("Empirical evidence", experiments.length ? experiments.map((x) => `${x.fault} broke ${x.impacted.length ? x.impacted.join(", ") : "nothing"}`) : empirical.map((e) => e.label),
      "No experiment on this service in this run."),
    h("div", { class: "grid2", style: { marginTop: "16px" } },
      h("div", h("h3", "Predicted dependent journeys"), h("div", { class: "chips" }, predicted.map((j) => h("span", { class: "chip", text: `${j} ${JOURNEY_SHORT[j]}` }))),
        h("p", { class: "meta", text: "From the deterministic journey map." })),
      h("div", h("h3", "Observed impact in this run"), observed.length ? h("div", { class: "chips" }, observed.map((j) => h("span", { class: "chip hit", text: `${j} ${JOURNEY_SHORT[j]}` })))
        : h("p", { class: "meta", text: "None recorded in this run." }),
        h("p", { class: "meta", text: "Observed impact is not a causal guarantee: a failed upload can block later checks (J5/J6) without a direct dependency." }))),
    h("span", { class: "meta", text: "Source: saved run topology + deterministic journey map" }));
}

async function render(ctx, alive) {
  const runId = ctx.currentRunId();
  if (!runId) return mount(ctx.el, viewHead({ title: "Dependency graph" }), emptyState({ title: "No topology yet", body: "The dependency map is saved with every run." }));
  const topo = await ctx.api.topology(runId);
  if (!alive()) return;
  if (!topo.available) {
    return mount(ctx.el, viewHead({ title: "Dependency graph", right: runPicker(runId) }),
      emptyState({ title: "Topology not available", body: "This run has no saved topology.json." }));
  }
  const undeclared = DEPS.filter((k) => topo.edges.some((e) => e.from === "server" && e.to === k) && !topo.edges.some((e) => e.source === "compose" && e.from === "server" && e.to === k));
  const detailSlot = h("div");
  const graphEl = h("div");
  const onSelect = (key) => {
    selectedNode = key;
    for (const b of graphEl.querySelectorAll("[data-node]")) b.setAttribute("aria-pressed", String(b.dataset.node === key));
    mount(detailSlot, detail(topo, key));
    announce(`Selected ${SERVICE[key] || key}`);
  };
  const userEdge = topo.edges.find((e) => e.from === "user" && e.to === "server");
  mount(graphEl, h("div", { class: "topology", role: "group", "aria-label": "Browser calls the Immich server, which depends on PostgreSQL, Redis and machine learning" },
    h("div", { class: "tnode static", "aria-hidden": "false" }, h("strong", "Browser / API"), h("small", "User journeys J0–J6")),
    h("div", { class: "tarrow", "aria-hidden": "true" }, "→", h("small", { text: userEdge ? userEdge.label : "HTTP" })),
    h("button", { type: "button", class: "tnode", "aria-pressed": String(selectedNode === "server"), "data-node": "server", onclick: () => onSelect("server") },
      h("strong", "Immich server"), h("small", "immich_server · API + job producer"), h("small", { text: `Observed impact: ${range(observedFor(topo, "server"))}` })),
    h("div", { class: "tarrow", "aria-hidden": "true" }, "→"),
    h("div", { class: "tdeps" }, DEPS.map((k) => h("div", { class: "tbranch" }, node(topo, k, onSelect))))));

  mount(ctx.el,
    viewHead({ title: "Dependency graph", lead: "One entry point, three backing services. Discovered from Compose, Immich's runtime configuration and the experiments of this run.",
      right: h("div", { class: "row" }, runPicker(runId), h("a", { class: "btn", href: "/graph", target: "_blank", rel: "noopener" }, icon("external-link"), "Generated diagrams")) }),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", h("h2", "Immich dependency map"), h("div", { class: "meta", text: "Arrows mean “calls / depends on”. Select a service to inspect its evidence." })),
        undeclared.length ? h("span", { class: "pill warn", text: `${undeclared.length} undeclared ${undeclared.length === 1 ? "dependency" : "dependencies"}` }) : null),
      graphEl,
      h("div", { class: "legend" }, h("span", "Solid outline · declared in Compose"), h("span", "Dashed outline · absent from Compose (found in runtime configuration)")),
      h("p", { class: "meta", text: "Saved topology of this run, not live service health." })),
    detailSlot);
  if (!["server", ...DEPS].includes(selectedNode)) selectedNode = "ml";
  onSelect(selectedNode);
  mount(ctx.foot, h("span", { class: "mono", text: runId }), h("span", { text: "Historical topology from topology.json" }));
}

export default { title: "Dependency graph", icon: "network", render };
