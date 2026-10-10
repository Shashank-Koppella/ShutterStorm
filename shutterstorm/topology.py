"""Dependency discovery, Mermaid output and deterministic impact predictions.

Edges come from three sources, and each edge says which one:

* ``compose``   - ``depends_on`` in ``docker compose config``
* ``config``    - runtime configuration: Immich's ``machineLearning.urls`` and the
                  server's DB/Redis host settings (env or Immich defaults)
* ``empirical`` - learned from experiments: a journey broke when a service was faulted

The server -> ML edge is *not* declared in compose; it is only visible in config
(and later confirmed empirically). That gap is one of our demo points.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .config import ML, ML2, POSTGRES, REDIS, SERVER
from .faults import FaultSpec

SERVICE_CONTAINERS = {"server": SERVER, "ml": ML, "redis": REDIS, "database": POSTGRES}
COMPOSE_TO_SERVICE = {"immich-server": "server", "immich-machine-learning": "ml", "redis": "redis",
                      "database": "database", "immich-machine-learning-2": "ml2"}

# Which backing services each user journey needs (the server is implied for all).
JOURNEY_DEPS: dict[str, list[str]] = {
    "J0": ["server"],
    "J1": ["server", "database"],
    "J2": ["server", "database"],
    "J3": ["server", "database", "ml"],
    "J4": ["server", "database", "redis"],
    "J5": ["server", "database", "redis"],
    "J6": ["server", "database"],
}


def _env_value(env_list: list[str], key: str) -> str | None:
    for item in env_list or []:
        if item.startswith(key + "="):
            return item.split("=", 1)[1]
    return None


def discover(ctx) -> dict:
    """Build the dependency map from compose, live config and container env."""
    edges: list[dict] = []
    nodes = {"user": "Browser / API client"}

    compose = ctx.docker.compose_config(ctx.settings.immich_dir)
    for name, svc in compose.get("services", {}).items():
        node = COMPOSE_TO_SERVICE.get(name, name)
        nodes[node] = f"{name} ({svc.get('container_name', name)})"
        for dep in (svc.get("depends_on") or {}):
            edges.append({"from": node, "to": COMPOSE_TO_SERVICE.get(dep, dep),
                          "label": "depends_on", "source": "compose"})
        if svc.get("ports"):
            edges.append({"from": "user", "to": node, "label": "HTTP :2283", "source": "compose"})

    # config: ML URLs from Immich's own system config
    try:
        ml_urls = ctx.admin().get_system_config()["machineLearning"]["urls"]
    except Exception:  # noqa: BLE001 - discovery should still work without the API
        ml_urls = []
    for url in ml_urls:
        host = urlparse(url).hostname or ""
        target = COMPOSE_TO_SERVICE.get(host, host)
        edges.append({"from": "server", "to": target, "label": f"HTTP {url} (machineLearning.urls)",
                      "source": "config"})

    # config: DB / Redis hosts from the server's environment (or Immich defaults)
    info = ctx.docker.inspect(SERVER) or {}
    env = info.get("Config", {}).get("Env", [])
    for key, default, port in (("DB_HOSTNAME", "database", 5432), ("REDIS_HOSTNAME", "redis", 6379)):
        host = _env_value(env, key) or default
        how = "env" if _env_value(env, key) else "Immich default"
        edges.append({"from": "server", "to": COMPOSE_TO_SERVICE.get(host, host),
                      "label": f"{key}={host}:{port} ({how})", "source": "config"})

    undeclared = [e for e in edges if e["source"] == "config" and not any(
        c["source"] == "compose" and c["from"] == e["from"] and c["to"] == e["to"] for c in edges)]
    return {
        "nodes": nodes,
        "edges": _dedupe(edges),
        "journey_deps": JOURNEY_DEPS,
        "undeclared_dependencies": [f"{e['from']} -> {e['to']} ({e['label']})" for e in undeclared],
        "ml2_running": ctx.docker.state(ML2)["running"],
    }


def _dedupe(edges: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in edges:
        key = (e["from"], e["to"], e["source"], e["label"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def add_empirical_edges(topology: dict, experiments: list[dict]) -> dict:
    """Add 'journey broke when service X was faulted' edges learned from experiments."""
    for exp in experiments:
        service = exp["service"]
        for journey in exp.get("actual_impacted", []):
            if journey == "J0" and service != "server":
                continue
            edge = {"from": "server", "to": service, "label": f"{journey} broke under {exp['fault']}",
                    "source": "empirical"}
            if service != "server" and edge not in topology["edges"]:
                topology["edges"].append(edge)
    return topology


def to_mermaid(topology: dict) -> str:
    """Mermaid flowchart. Solid = compose, dashed = config, thick = empirical."""
    lines = ["graph LR"]
    for node, label in topology["nodes"].items():
        lines.append(f'    {node}["{label}"]')
    arrows = {"compose": "-->", "config": "-.->", "empirical": "==>"}
    for e in topology["edges"]:
        label = f"{e['source']}: {e['label']}".replace('"', "'")
        lines.append(f'    {e["from"]} {arrows[e["source"]]}|"{label}"| {e["to"]}')
    return "\n".join(lines)


def predict_impact(spec: FaultSpec, topology: dict) -> dict:
    """Deterministic prediction: journeys whose dependencies include the faulted service."""
    deps = topology.get("journey_deps", JOURNEY_DEPS)
    if spec.service == "server":
        broken = list(deps)
        reason = "every journey goes through the API server"
    else:
        broken = [j for j, services in deps.items() if spec.service in services]
        reason = f"journeys that depend on {spec.service} per the dependency map"
    if spec.kind == "cpu":
        reason += " (expected to slow down rather than fail)"
    return {"predicted_impacted": broken, "reason": reason, "source": "dependency map"}


def score_prediction(predicted: list[str], actual: list[str]) -> dict:
    """True/false positives and negatives for one experiment."""
    p, a = set(predicted), set(actual)
    return {"tp": sorted(p & a), "fp": sorted(p - a), "fn": sorted(a - p)}


def prediction_scorecard(experiments: list[dict]) -> dict:
    """Aggregate precision and recall over all experiments."""
    tp = sum(len(e["prediction_check"]["tp"]) for e in experiments)
    fp = sum(len(e["prediction_check"]["fp"]) for e in experiments)
    fn = sum(len(e["prediction_check"]["fn"]) for e in experiments)
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": round(tp / (tp + fp), 2) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 2) if tp + fn else 1.0,
        "mismatches": [
            {"fault": e["fault"], "unexpected_impact": e["prediction_check"]["fn"],
             "predicted_but_fine": e["prediction_check"]["fp"]}
            for e in experiments if e["prediction_check"]["fp"] or e["prediction_check"]["fn"]
        ],
    }
