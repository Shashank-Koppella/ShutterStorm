"""Read-only JSON views for the dashboard (Stormglass frontend).

Pure functions over the ``runs/`` folder and the state folder, so they are testable without
HTTP. Rules (docs/design/STORMGLASS.md):

* run ids are validated against a strict pattern AND must name an existing run folder of the
  current mode: simulated and real runs are never mixed;
* nothing is invented: missing evidence is returned as ``None`` / ``available: false``;
* raw probe lists, log tails and secrets are not exposed; text is returned as data and the
  browser renders it as text.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import Settings
from .events import read_events
from .faults import CATALOG, FAULT_KNOBS
from .planner import DURATION_BOUNDS, ERROR_RATE_FLOOR, P95_FLOOR_MS
from .probes import JOURNEYS

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9_-]{1,64}$")
RESULT_KINDS = ("run",)
SKIP_EXPERIMENT_KEYS = {"fault_results", "recovery_results", "log_tails", "queues", "baseline_summary"}


# ---------------------------------------------------------------- run folders
def is_sim_name(name: str) -> bool:
    return name.endswith("-SIM") or bool(re.search(r"-SIM-\d+$", name))


def run_dir(settings: Settings, run_id: str) -> Path | None:
    """The folder of ``run_id`` if it is a valid, existing run of the current mode, else None."""
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or ".." in run_id:
        return None
    if is_sim_name(run_id) != settings.simulate:
        return None
    path = (settings.runs_dir / run_id)
    try:
        path = path.resolve()
        if settings.runs_dir.resolve() not in path.parents or not path.is_dir():
            return None
    except OSError:
        return None
    return path


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _kind(name: str) -> str:
    rest = name[16:]  # after "YYYYMMDD-HHMMSS-"
    rest = re.sub(r"-SIM(-\d+)?$|-\d+$", "", rest)
    return "inject" if rest.startswith("inject-") else rest


def list_runs(settings: Settings, limit: int = 60) -> list[dict]:
    """Result runs (with findings.json) of the current mode, newest first."""
    out = []
    if not settings.runs_dir.exists():
        return out
    for d in sorted(settings.runs_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or not RUN_ID_RE.match(d.name) or is_sim_name(d.name) != settings.simulate:
            continue
        if _kind(d.name) not in RESULT_KINDS:
            continue
        data = _load(d / "findings.json")
        if not isinstance(data, dict):
            continue
        exps = data.get("experiments") or []
        decisions = data.get("decisions") or []
        cleanup = data.get("cleanup")
        out.append({
            "id": d.name,
            "status": data.get("status"),
            "started": data.get("started"),
            "finished": data.get("finished"),
            "overall_score": data.get("overall_score"),
            "experiments": len(exps),
            "faults": [e.get("fault") for e in exps],
            "auto_stops": sum(1 for e in exps if e.get("auto_stopped")),
            "cleanup_ok": cleanup.get("ok") if isinstance(cleanup, dict) and cleanup.get("checks") else None,
            "plan_source": (data.get("plan") or {}).get("source"),
            "ai_decisions": sum(1 for x in decisions if x.get("source") in ("live", "cache")),
            "plan_changes": sum(1 for x in decisions if x.get("followed_plan") is False),
            "simulated": bool(data.get("simulated")),
            "has_events": (d / "events.jsonl").exists(),
            "has_report": (d / "report.html").exists(),
            "has_comparison": bool((data.get("comparison") or {}).get("rows")),
        })
        if len(out) >= limit:
            break
    return out


def run_detail(settings: Settings, run_id: str) -> dict | None:
    d = run_dir(settings, run_id)
    if d is None:
        return None
    data = _load(d / "findings.json")
    if not isinstance(data, dict):
        return None
    exps = []
    for e in data.get("experiments") or []:
        x = {k: v for k, v in e.items() if k not in SKIP_EXPERIMENT_KEYS}
        if isinstance(x.get("integrity"), dict):
            x["integrity"] = {k: v for k, v in x["integrity"].items() if k != "details"}
        if isinstance(x.get("safety"), dict):
            x["safety"] = {k: v for k, v in x["safety"].items()}
        x["has_evidence"] = (d / "evidence" / str(e.get("label")) / "evidence.json").exists()
        exps.append(x)
    exps.sort(key=lambda e: (e.get("scoring") or {}).get("severity", 0), reverse=True)  # backend severity order
    report = data.get("report") or {}
    cleanup = data.get("cleanup") if isinstance(data.get("cleanup"), dict) else None
    plan = {k: v for k, v in (data.get("plan") or {}).items() if k != "candidates"}
    return {
        "id": d.name,
        "kind": _kind(d.name),
        "simulated": bool(data.get("simulated")),
        "status": data.get("status"),
        "error": data.get("error"),
        "started": data.get("started"),
        "finished": data.get("finished"),
        "overall_score": data.get("overall_score"),
        "llm_mode": data.get("llm_mode"),
        "llm_stats": data.get("llm_stats"),
        "thresholds": data.get("thresholds"),
        "plan": plan,
        "decisions": data.get("decisions") or plan.get("decisions") or [],
        "scorecard": data.get("scorecard"),
        "cleanup": cleanup,
        "comparison": data.get("comparison"),
        "report": {k: report.get(k) for k in ("executive_summary", "recommendations", "source")},
        "baseline_summary": data.get("baseline_summary"),
        "experiments": exps,
        "has_events": (d / "events.jsonl").exists(),
        "has_topology": (d / "topology.json").exists(),
        "has_report": (d / "report.html").exists(),
        "journeys": JOURNEYS,
    }


def timeline(settings: Settings, run_id: str, label: str) -> dict | None:
    """Per-journey probe series of one experiment, times relative to the injection."""
    d = run_dir(settings, run_id)
    if d is None or not isinstance(label, str) or not re.match(r"^[a-z0-9_]{1,64}$", label):
        return None
    ev = _load(d / "evidence" / label / "evidence.json")
    if not isinstance(ev, dict):
        return None
    fault = [r for r in ev.get("fault_results", []) if isinstance(r, dict)]
    recov = [r for r in ev.get("recovery_results", []) if isinstance(r, dict)]
    if ev.get("applied_at"):
        t0, t0_source = float(ev["applied_at"]), "injection"
    elif fault:
        t0, t0_source = min(r["t"] - r["latency_ms"] / 1000 for r in fault), "first fault probe"
    else:
        return None
    undo = (float(ev["undone_at"]) - t0) if ev.get("undone_at") else (max(r["t"] for r in fault) - t0 if fault else None)
    series: dict[str, list] = {j: [] for j in JOURNEYS}
    for phase, rows in (("fault", fault), ("recovery", recov)):
        for r in rows:
            if r.get("journey") in series:
                outcome = "ok" if r.get("ok") else ("hung" if r.get("timed_out") else "fast-fail")
                series[r["journey"]].append({"t": round(r["t"] - t0, 2), "latency_ms": r.get("latency_ms"),
                                             "outcome": outcome, "phase": phase})
    for pts in series.values():
        pts.sort(key=lambda p: p["t"])
    auto = ev.get("auto_stopped") or {}
    return {
        "label": label, "fault": ev.get("fault"), "t0_source": t0_source, "window_s": ev.get("window_s"),
        "undo_t": round(undo, 2) if undo is not None else None, "undo_exact": bool(ev.get("undone_at")),
        "auto_stop_after_s": auto.get("after_s"), "auto_stop_reason": auto.get("reason"),
        "recovery_s": ev.get("recovery_s"), "timeout_ms": settings.probe_timeout_seconds * 1000,
        "safety_rounds": (ev.get("safety") or {}).get("rounds", []),
        "limits": (ev.get("safety") or {}).get("thresholds"),
        "series": series,
    }


def topology(settings: Settings, run_id: str) -> dict | None:
    """The saved, validated dependency map of a run plus what each service's experiments broke."""
    d = run_dir(settings, run_id)
    if d is None:
        return None
    topo = _load(d / "topology.json")
    if not isinstance(topo, dict):
        return {"available": False, "id": d.name}
    nodes = {str(k): str(v) for k, v in (topo.get("nodes") or {}).items() if isinstance(k, str)}
    edges = [{k: str(e.get(k, "")) for k in ("from", "to", "source", "label")}
             for e in topo.get("edges") or [] if isinstance(e, dict) and e.get("source") in ("compose", "config", "empirical")]
    deps = {str(j): [str(s) for s in v] for j, v in (topo.get("journey_deps") or {}).items() if isinstance(v, list)}
    findings = _load(d / "findings.json") or {}
    impacts: dict[str, list] = {}
    for e in findings.get("experiments") or []:
        spec = CATALOG.get(e.get("fault"))
        if spec is None:
            continue
        impacts.setdefault(spec.service, []).append({"fault": e.get("fault"), "impacted": e.get("actual_impacted", []),
                                                     "heatmap": e.get("heatmap", {})})
    return {"available": True, "id": d.name, "nodes": nodes, "edges": edges, "journey_deps": deps,
            "undeclared": [str(u) for u in topo.get("undeclared_dependencies") or []], "impacts": impacts,
            "journeys": JOURNEYS}


def events(settings: Settings, run_id: str, after: int = 0) -> dict | None:
    d = run_dir(settings, run_id)
    if d is None:
        return None
    evs = read_events(d / "events.jsonl", after=max(0, int(after)))
    return {"id": d.name, "events": evs, "last": evs[-1]["seq"] if evs else after}


def latest_plan(settings: Settings) -> dict | None:
    """The newest dry-run plan (``plan`` command) of the current mode, with candidate features."""
    if not settings.runs_dir.exists():
        return None
    for d in sorted(settings.runs_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or not RUN_ID_RE.match(d.name) or is_sim_name(d.name) != settings.simulate:
            continue
        if _kind(d.name) != "plan":
            continue
        p = _load(d / "plan.json")
        if not isinstance(p, dict) or not p.get("items"):
            continue
        cands = [{k: c.get(k) for k in ("fault", "priority", "risk", "novelty", "failure_class", "available",
                                        "unavailable_reason", "primary_service", "kind", "undeclared_dependency",
                                        "hang_style", "predicted_impacted")}
                 for c in p.get("candidates") or [] if isinstance(c, dict)]
        return {"id": d.name, "created": d.stat().st_mtime, "source": p.get("source"), "strategy": p.get("strategy"),
                "budget": p.get("budget"), "items": p.get("items"), "skipped": p.get("skipped"),
                "deterministic": p.get("deterministic"), "candidates": cands}
    return None


def catalog(settings: Settings) -> dict:
    t = settings.thresholds
    return {
        "faults": [{"id": f.id, "description": f.description, "kind": f.kind, "service": f.service,
                    "optional": f.optional, "parts": list(f.parts),
                    "knob": (dict(zip(("name", "default", "min", "max"), FAULT_KNOBS[f.kind]))
                             if f.kind in FAULT_KNOBS else None)} for f in CATALOG.values()],
        "caps": {"max_error_rate": t.max_error_rate, "max_p95_ms": t.max_p95_ms, "rounds": t.rounds,
                 "blast_radius": t.blast_radius, "describe": t.describe()},
        "floors": {"max_error_rate": ERROR_RATE_FLOOR, "max_p95_ms": P95_FLOOR_MS},
        "duration_bounds": list(DURATION_BOUNDS),
        "max_fault_seconds": settings.max_fault_seconds,
        "journeys": JOURNEYS,
    }


def safety_now(settings: Settings, journal) -> dict:
    """Live safety state: pending undo entries, guard liveness, STOP file. Facts only."""
    state = journal.state_dir
    entries = []
    for e in journal.entries():
        entries.append({"label": e.get("label"), "kind": e.get("kind"), "recipe": (e.get("recipe") or {}).get("type"),
                        "created_at": e.get("created_at"), "deadline": e.get("deadline"), "owner": e.get("owner")})
    guard: dict = {"seen": False}
    try:
        age = time.time() - float((state / "guard.heartbeat").read_text())
        guard = {"seen": True, "mode": "container", "heartbeat_age_s": round(age, 1), "alive": age < 10}
    except (OSError, ValueError):
        pid_file = state / "guard.pid"
        if pid_file.exists():
            from .guard import pid_alive
            try:
                alive = pid_alive(int(pid_file.read_text().strip()))
            except ValueError:
                alive = False
            guard = {"seen": True, "mode": "process", "alive": alive}
    return {"journal": entries, "guard": guard, "stop_file": journal.stop_requested(),
            "caps": settings.thresholds.describe()}
