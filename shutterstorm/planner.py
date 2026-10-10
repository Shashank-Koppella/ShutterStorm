"""AI experiment planner: rank and select experiments from the dependency map.

The Planner agent (LLM) chooses *which* catalog faults to run and in what order, with a
short reason for each. It can only choose from the fixed catalog (anything else fails
validation), so the AI decides what is worth testing but never what is safe to do.

Every candidate gets deterministic features first (from the discovered dependency map,
the scoring weights and earlier runs), and a deterministic ranking that is used whenever
the LLM is off, unavailable or gives an invalid plan:

    risk     = criticality x likelihood x (0.4 + 0.6 x blast radius)     (normalised to 0..1)
    novelty  = 1.0 if never tested, else 0.4 + min(0.6, 3 x last severity)
    priority = 0.55 x risk + 0.45 x novelty
               + 0.15 if the faulted dependency is NOT declared in compose (found in live config)
               + 0.10 for hang-style faults (silent failures are the worst kind)
               + 0.10 for combined failures (cascades the map cannot predict)

Selection: first the best candidate for every service in the dependency map (coverage),
then the rest by priority, with at least ``MIN_CLASSES`` different failure classes, at most
``MAX_PER_SERVICE`` experiments per service and ``MAX_COMBOS`` combined failure. The result
runs in priority order (riskiest / most informative first).
"""
from __future__ import annotations

import json
from pathlib import Path

from .agents import _ask, _need, grounding_errors
from .faults import CATALOG, NETEM_ARGS, NETEM_IMAGE, FaultSpec
from .llm import LLMClient
from .probes import ALL_JOURNEYS
from .scoring import CRITICALITY, LIKELIHOOD
from .topology import predict_impact

FAILURE_CLASS = {"pause": "hung dependency", "stop": "dependency down", "restart": "container crash",
                 "cpu": "resource exhaustion", "latency": "network fault", "loss": "network fault",
                 "load": "traffic spike", "combo": "combined failure"}
HANG_STYLE = {"pause", "latency", "loss"}
MIN_CLASSES = 3
MAX_PER_SERVICE = 2
MAX_COMBOS = 1
DEFAULT_BUDGET = 6


# ====================================================================== features
def run_history(runs_dir: Path, simulate: bool) -> dict[str, dict]:
    """Per fault: how often it ran in earlier runs (real or simulated, never mixed) and its last result."""
    history: dict[str, dict] = {}
    for d in sorted(runs_dir.glob("*-run*")):
        if d.name.endswith("-SIM") != simulate or not (d / "findings.json").exists():
            continue
        try:
            data = json.loads((d / "findings.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for e in data.get("experiments", []):
            h = history.setdefault(e["fault"], {"runs": 0})
            h["runs"] += 1
            h.update(last_run=d.name, last_score=e["scoring"]["score"], last_severity=e["scoring"]["severity"],
                     last_impacted=e.get("actual_impacted", []))
    return history


def candidates(ctx, topology: dict, history: dict[str, dict]) -> list[dict]:
    """Every catalog fault with the features the planner reasons about."""
    undeclared = " ".join(topology.get("undeclared_dependencies", []))
    netem_ok = None
    out = []
    for spec in CATALOG.values():
        available, why = True, ""
        if spec.optional:
            available, why = False, "optional fault: run it explicitly with --experiments"
        elif any(p.kind in NETEM_ARGS for p in spec.atomic):
            netem_ok = ctx.docker.image_present(NETEM_IMAGE) if netem_ok is None else netem_ok
            if not netem_ok:
                available, why = False, f"needs the {NETEM_IMAGE} image"
        predicted = predict_impact(spec, topology)["predicted_impacted"]
        h = history.get(spec.id, {"runs": 0})
        out.append({
            "fault": spec.id,
            "description": spec.description,
            "failure_class": FAILURE_CLASS[spec.kind],
            "kind": spec.kind,
            "services": list(spec.services),
            "primary_service": spec.service,
            "criticality": CRITICALITY[spec.service],
            "likelihood": LIKELIHOOD[spec.kind],
            "predicted_impacted": predicted,
            "blast_radius": round(len(predicted) / len(ALL_JOURNEYS), 2),
            "undeclared_dependency": any(f"server -> {s} " in undeclared + " " for s in spec.services),
            "hang_style": any(p.kind in HANG_STYLE for p in spec.atomic),
            "history": h,
            "available": available,
            "unavailable_reason": why,
        })
    _score(out)
    return out


def _score(cands: list[dict]) -> None:
    risks = {c["fault"]: c["criticality"] * c["likelihood"] * (0.4 + 0.6 * c["blast_radius"]) for c in cands}
    top = max(risks.values()) or 1.0
    for c in cands:
        h = c["history"]
        novelty = 1.0 if not h["runs"] else 0.4 + min(0.6, 3 * h.get("last_severity", 0.0))
        bonus = (0.15 if c["undeclared_dependency"] else 0) + (0.10 if c["hang_style"] else 0) \
            + (0.10 if c["kind"] == "combo" else 0)
        c["risk"] = round(risks[c["fault"]] / top, 3)
        c["novelty"] = round(novelty, 3)
        c["priority"] = round(0.55 * c["risk"] + 0.45 * novelty + bonus, 3)
        c["deterministic_reason"] = _reason(c)


def _reason(c: dict) -> str:
    bits = [f"{c['failure_class']} on {'+'.join(c['services'])} (criticality {c['criticality']})"]
    if c["undeclared_dependency"]:
        bits.append("server depends on it although compose does not declare it")
    if c["hang_style"]:
        bits.append("hang-style faults tend to fail silently")
    if c["kind"] == "combo":
        bits.append("tests a cascade the dependency map cannot predict")
    bits.append(f"predicted to affect {', '.join(c['predicted_impacted']) or 'no journey'}")
    h = c["history"]
    bits.append("never tested" if not h["runs"] else
                f"tested {h['runs']}x before, last severity {h.get('last_severity')}")
    return "; ".join(bits)


# ====================================================================== selection
def select(ordered: list[dict], budget: int) -> list[dict]:
    """Pick from ``ordered`` (best first): cover every service, then fill with constraints."""
    pool = [c for c in ordered if c["available"]]
    min_classes = min(MIN_CLASSES, len({c["failure_class"] for c in pool}))
    picked: list[dict] = []

    def allowed(c: dict) -> bool:
        if c in picked or len(picked) >= budget:
            return False
        if sum(p["primary_service"] == c["primary_service"] for p in picked) >= MAX_PER_SERVICE:
            return False
        if c["kind"] == "combo" and sum(p["kind"] == "combo" for p in picked) >= MAX_COMBOS:
            return False
        classes = {p["failure_class"] for p in picked}
        missing = min_classes - len(classes)
        # the remaining slots are reserved for failure classes not covered yet
        return not (missing > 0 and budget - len(picked) <= missing and c["failure_class"] in classes)

    for service in dict.fromkeys(c["primary_service"] for c in pool):  # 1) coverage of the map
        best = next((c for c in pool if c["primary_service"] == service and c["kind"] != "combo" and allowed(c)), None)
        if best:
            picked.append(best)
    for c in pool:                                                       # 2) fill by priority
        if allowed(c):
            picked.append(c)
    rank = {c["fault"]: n for n, c in enumerate(ordered)}
    return sorted(picked, key=lambda c: rank[c["fault"]])


def deterministic_plan(cands: list[dict], budget: int) -> list[dict]:
    ordered = sorted(cands, key=lambda c: (-c["priority"], c["fault"]))
    return [{"fault": c["fault"], "reason": c["deterministic_reason"]} for c in select(ordered, budget)]


# ====================================================================== LLM planner
def check_plan(data: dict, cands: list[dict], budget: int) -> list[str]:
    errors = _need(data, {"plan": list})
    if errors:
        return errors
    allowed = {c["fault"]: c for c in cands if c["available"]}
    seen = []
    for item in data["plan"]:
        if not isinstance(item, dict) or not isinstance(item.get("fault"), str) or not isinstance(item.get("reason"), str):
            return ["each plan item must be {\"fault\": str, \"reason\": str}"]
        if item["fault"] not in allowed:
            errors.append(f"{item['fault']!r} is not an available catalog fault")
        elif item["fault"] in seen:
            errors.append(f"{item['fault']} is listed twice")
        elif not item["reason"].strip():
            errors.append(f"{item['fault']} has no reason")
        seen.append(item["fault"])
    if errors:
        return errors
    if len(seen) != min(budget, len(allowed)):
        errors.append(f"choose exactly {min(budget, len(allowed))} experiments (got {len(seen)})")
    classes = {allowed[f]["failure_class"] for f in seen}
    need = min(MIN_CLASSES, len({c["failure_class"] for c in allowed.values()}))
    if len(classes) < need:
        errors.append(f"cover at least {need} different failure classes (got {sorted(classes)})")
    if sum(allowed[f]["kind"] == "combo" for f in seen) > MAX_COMBOS:
        errors.append(f"at most {MAX_COMBOS} combined failure")
    for service in {allowed[f]["primary_service"] for f in seen}:
        if sum(allowed[f]["primary_service"] == service for f in seen) > MAX_PER_SERVICE:
            errors.append(f"at most {MAX_PER_SERVICE} experiments on {service}")
    return errors


def plan(ctx, llm: LLMClient, topology: dict, budget: int = DEFAULT_BUDGET) -> dict:
    """Return the experiment plan: ordered faults with reasons, plus every candidate's features."""
    history = run_history(ctx.settings.runs_dir, ctx.simulate)
    cands = candidates(ctx, topology, history)
    fallback = deterministic_plan(cands, budget)
    payload = {
        "dependency_edges": [{k: e[k] for k in ("from", "to", "source", "label")} for e in topology["edges"]],
        "undeclared_dependencies": topology.get("undeclared_dependencies", []),
        "journeys": {j: deps for j, deps in topology.get("journey_deps", {}).items()},
        "budget": min(budget, sum(c["available"] for c in cands)),
        "candidates": [{k: c[k] for k in ("fault", "description", "failure_class", "services", "criticality",
                                          "likelihood", "predicted_impacted", "blast_radius",
                                          "undeclared_dependency", "hang_style", "history", "priority")}
                       for c in cands if c["available"]],
        "deterministic_suggestion": [p["fault"] for p in fallback],
    }
    instruction = (
        "You are the experiment Planner of a chaos-engineering tool. Using the dependency map and the candidate "
        "features, choose which experiments to run and in which order: the most informative or riskiest first. "
        "Prefer dependencies that compose does not declare, faults that may fail silently (hangs), high "
        "criticality x likelihood, wide blast radius, and faults that were never tested or were severe before. "
        f"Choose exactly the budget, only fault ids from the candidates, cover at least {MIN_CLASSES} "
        f"different failure classes, every service of the map if the budget allows, at most {MAX_PER_SERVICE} "
        f"experiments per primary service and at most {MAX_COMBOS} combined failure. You may deviate from "
        "deterministic_suggestion when you explain why. Schema: "
        "{\"plan\": [{\"fault\": str, \"reason\": str (one sentence that names the dependency or journeys "
        "involved)}], \"strategy\": str (one sentence)}")
    data, source = _ask(llm, instruction, payload,
                        lambda d: check_plan(d, cands, budget) or grounding_errors(
                            {"r": [i.get("reason", "") for i in d["plan"]] + [d.get("strategy", "")]}, ["r"], payload))
    if data is None:
        chosen, strategy, source = fallback, ("Deterministic ranking (no valid AI plan): priority = "
                                              "0.55 risk + 0.45 novelty + bonuses, with failure-class diversity."), "fallback"
    else:
        chosen = [{"fault": i["fault"], "reason": i["reason"].strip()} for i in data["plan"]]
        strategy = str(data.get("strategy", "")).strip()
    by_id = {c["fault"]: c for c in cands}
    items = [{"rank": n, "fault": i["fault"], "reason": i["reason"],
              "deterministic_reason": by_id[i["fault"]]["deterministic_reason"],
              "priority": by_id[i["fault"]]["priority"], "failure_class": by_id[i["fault"]]["failure_class"],
              "predicted_impacted": by_id[i["fault"]]["predicted_impacted"]}
             for n, i in enumerate(chosen, 1)]
    picked = {i["fault"] for i in items}
    skipped = [{"fault": c["fault"], "priority": c["priority"],
                "why": c["unavailable_reason"] or "not selected: lower priority, or the budget "
                                                  f"({budget}), per-service ({MAX_PER_SERVICE}) or combo "
                                                  f"({MAX_COMBOS}) limit was reached"}
               for c in sorted(cands, key=lambda c: -c["priority"]) if c["fault"] not in picked]
    return {"source": source, "strategy": strategy, "budget": budget, "items": items, "skipped": skipped,
            "deterministic": [p["fault"] for p in fallback], "candidates": cands}


def spec_list(p: dict) -> list[FaultSpec]:
    return [CATALOG[i["fault"]] for i in p["items"]]
