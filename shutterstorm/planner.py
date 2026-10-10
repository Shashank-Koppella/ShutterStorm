"""AI experiment planner: the AI decides what to break, when, and how safely.

* **What**: the Planner agent (LLM) ranks the fixed fault catalog on the dependency map and
  writes a tentative plan with a reason per experiment (:func:`plan`).
* **When**: before EVERY experiment it decides again (:func:`decide_next`), seeing what the
  experiments of this run have shown so far; it may follow or change its tentative plan.
* **How safely**: for each experiment it also picks the fault strength (CPU limit, number of
  traffic clients, network delay or loss), the fault duration and the safety limits that
  roll the fault back (:func:`decide_settings`).

Hard rules the AI cannot break (enforced in code, not in the prompt): only catalog faults;
strength and duration are clamped to fixed bounds; safety limits can only be made STRICTER
than the configured caps, never looser; blast-radius checking, TTL, guard and journal always
apply. Any invalid answer -> one retry -> the deterministic choice below.

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
from .config import Thresholds
from .faults import CATALOG, FAULT_KNOBS, NETEM_ARGS, NETEM_IMAGE, FaultSpec
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
DURATION_BOUNDS = (15.0, 45.0)        # fault window the AI may choose, seconds
ERROR_RATE_FLOOR = 0.25               # stricter than this would stop almost every experiment at once
P95_FLOOR_MS = 2000.0


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
def allowed_next(c: dict, picked: list[dict], budget: int, min_classes: int) -> bool:
    """May ``c`` be the next experiment after ``picked``, keeping every selection rule satisfiable?"""
    if not c["available"] or any(p["fault"] == c["fault"] for p in picked) or len(picked) >= budget:
        return False
    if sum(p["primary_service"] == c["primary_service"] for p in picked) >= MAX_PER_SERVICE:
        return False
    if c["kind"] == "combo" and sum(p["kind"] == "combo" for p in picked) >= MAX_COMBOS:
        return False
    classes = {p["failure_class"] for p in picked}
    missing = min_classes - len(classes)
    # the remaining slots are reserved for failure classes not covered yet
    return not (missing > 0 and budget - len(picked) <= missing and c["failure_class"] in classes)


def min_classes_for(cands: list[dict], budget: int) -> int:
    return min(MIN_CLASSES, budget, len({c["failure_class"] for c in cands if c["available"]}))


def select(ordered: list[dict], budget: int) -> list[dict]:
    """Pick from ``ordered`` (best first): cover every service, then fill with constraints."""
    pool = [c for c in ordered if c["available"]]
    min_classes = min_classes_for(pool, budget)
    picked: list[dict] = []

    def allowed(c: dict) -> bool:
        return allowed_next(c, picked, budget, min_classes)

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
    need = min_classes_for(list(allowed.values()), budget)
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


# ====================================================================== how safely: settings
def setting_bounds(spec: FaultSpec, caps: Thresholds, max_fault_s: float,
                   default_window: float = 35.0) -> dict[str, tuple]:
    """Every setting the AI may choose for ``spec``: name -> (default, low, high)."""
    hi_dur = min(DURATION_BOUNDS[1], max_fault_s - 5)
    default_dur = min(default_window, hi_dur)
    bounds = {
        "duration_s": (default_dur, min(DURATION_BOUNDS[0], default_dur), hi_dur),
        # safety limits: the configured value is the CAP; the AI may only go stricter (lower)
        "max_error_rate": (caps.max_error_rate, min(ERROR_RATE_FLOOR, caps.max_error_rate), caps.max_error_rate),
        "max_p95_ms": (caps.max_p95_ms, min(P95_FLOOR_MS, caps.max_p95_ms), caps.max_p95_ms),
    }
    for part in spec.atomic:
        if part.kind in FAULT_KNOBS:
            name, default, lo, hi = FAULT_KNOBS[part.kind]
            bounds[name] = (default, lo, hi)
    return bounds


def decide_settings(spec: FaultSpec, raw: dict | None, caps: Thresholds, max_fault_s: float,
                    source: str = "default", default_window: float = 35.0) -> dict:
    """Clamp the AI's requested settings to the hard bounds; record every adjustment."""
    raw = raw if isinstance(raw, dict) else {}
    chosen, notes = {}, []
    for name, (default, lo, hi) in setting_bounds(spec, caps, max_fault_s, default_window).items():
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if name in raw:
                notes.append(f"{name}={raw[name]!r} is not a number -> default {default}")
            value = default
        clamped = min(hi, max(lo, float(value)))
        if clamped != float(value):
            notes.append(f"{name} {value} -> {clamped:g} (allowed {lo:g}..{hi:g}"
                         + (", safety limits may only be stricter than the configured cap" if name.startswith("max_") else "")
                         + ")")
        chosen[name] = int(clamped) if name in ("workers", "delay_ms", "loss_pct") else round(clamped, 3)
    ignored = sorted(set(raw) - set(chosen))
    if ignored:
        notes.append(f"ignored unknown settings: {', '.join(ignored)}")
    thresholds = Thresholds(max_error_rate=chosen["max_error_rate"], max_p95_ms=chosen["max_p95_ms"],
                            blast_radius=caps.blast_radius, rounds=caps.rounds)
    params = {k: v for k, v in chosen.items() if k not in ("duration_s", "max_error_rate", "max_p95_ms")}
    return {"settings": chosen, "params": params, "duration_s": chosen["duration_s"], "thresholds": thresholds,
            "safety": thresholds.describe(), "clamped": notes, "settings_source": source}


# ====================================================================== when: the next experiment
def observation(exp: dict) -> dict:
    """What one finished experiment showed, compact, for the next decision."""
    return {
        "fault": exp["fault"],
        "heatmap": exp["heatmap"],
        "score": exp["scoring"]["score"],
        "severity": exp["scoring"]["severity"],
        "auto_stopped": (exp.get("auto_stopped") or {}).get("reason"),
        "recovery_s": exp.get("recovery_s"),
        "unexpected_impact": exp.get("prediction_check", {}).get("fn", []),
        "predicted_but_fine": exp.get("prediction_check", {}).get("fp", []),
        "ghost_uploads": exp.get("integrity", {}).get("ghost_uploads", 0),
        "settings_used": (exp.get("decision") or {}).get("settings"),
    }


def deterministic_next(cands: list[dict], plan_items: list[dict], done: list[dict], budget: int) -> dict | None:
    """Fallback 'what next': the next tentative-plan item that is still allowed, else the best allowed
    candidate, boosting services that were hit hard earlier in this run (dig deeper where it hurts)."""
    picked = [c for f in done for c in cands if c["fault"] == f["fault"]]
    min_classes = min_classes_for(cands, budget)
    by_id = {c["fault"]: c for c in cands}
    for item in plan_items:
        c = by_id.get(item["fault"])
        if c and allowed_next(c, picked, budget, min_classes):
            return {"fault": c["fault"], "reason": item["reason"], "from_plan": True}
    hurt = {f["fault"]: f for f in done if f["severity"] >= 0.07}
    hurt_services = {by_id[f]["primary_service"] for f in hurt if f in by_id}
    pool = [c for c in cands if allowed_next(c, picked, budget, min_classes)]
    if not pool:
        return None
    best = max(pool, key=lambda c: c["priority"] + (0.2 if c["primary_service"] in hurt_services else 0))
    return {"fault": best["fault"], "reason": best["deterministic_reason"], "from_plan": False}


def check_decision(d: dict, allowed: set[str]) -> list[str]:
    errors = _need(d, {"fault": str, "reason": str, "safety_reason": str})
    if errors:
        return errors
    if d["fault"] not in allowed:
        errors.append(f"{d['fault']!r} is not allowed next; choose one of {sorted(allowed)}")
    if not d["reason"].strip() or not d["safety_reason"].strip():
        errors.append("reason and safety_reason must not be empty")
    if "settings" in d and not isinstance(d["settings"], dict):
        errors.append("settings must be an object of numbers")
    return errors


def decide_next(ctx, llm: LLMClient, the_plan: dict, done_exps: list[dict], caps: Thresholds,
                budget: int) -> dict | None:
    """Decide the next experiment (what), now (when), and with which strength / limits (how safely)."""
    cands = the_plan["candidates"]
    done = [observation(e) for e in done_exps]
    picked = [c for f in done for c in cands if c["fault"] == f["fault"]]
    min_classes = min_classes_for(cands, budget)
    pool = [c for c in cands if allowed_next(c, picked, budget, min_classes)]
    if len(done) >= budget or not pool:
        return None
    remaining_plan = [i for i in the_plan["items"] if i["fault"] not in {f["fault"] for f in done}]
    fallback = deterministic_next(cands, remaining_plan, done, budget)
    bounds = {c["fault"]: {k: {"default": v[0], "min": v[1], "max": v[2]}
                           for k, v in setting_bounds(CATALOG[c["fault"]], caps, ctx.settings.max_fault_seconds,
                                                      ctx.settings.timings.fault_window_s).items()}
              for c in pool}
    payload = {
        "step": len(done) + 1, "budget": budget,
        "observed_so_far_in_this_run": done,
        "tentative_plan_remaining": [{"fault": i["fault"], "reason": i["reason"]} for i in remaining_plan],
        "allowed_next": [{k: c[k] for k in ("fault", "description", "failure_class", "services", "criticality",
                                            "predicted_impacted", "hang_style", "priority")} for c in pool],
        "settings_bounds": bounds,
        "safety_rules": "max_error_rate and max_p95_ms are CAPS: you may only choose stricter (lower) values. "
                        "Blast-radius checking, the 60 s TTL and the guard always apply.",
        "deterministic_suggestion": fallback["fault"] if fallback else None,
    }
    instruction = (
        "You are the experiment Planner of a chaos-engineering tool, deciding the NEXT experiment while the run is "
        "in progress. Use what was observed so far in this run: dig deeper where something broke unexpectedly or "
        "hung, avoid repeating what is already clear, keep covering failure classes. Decide (1) WHAT: one fault id "
        "from allowed_next (you may change the tentative plan), (2) HOW HARD and HOW LONG: settings within "
        "settings_bounds for that fault (duration_s, plus its strength setting if settings_bounds lists one: cpus, "
        "workers, delay_ms or loss_pct), and (3) HOW SAFELY: "
        "max_error_rate and max_p95_ms, stricter for risky targets (e.g. the database, or a service that already "
        "hung) and never above their max. Schema: {\"fault\": str, \"reason\": str (one sentence: why this "
        "experiment now, citing what was observed), \"settings\": {\"duration_s\": number, \"max_error_rate\": "
        "number, \"max_p95_ms\": number, plus cpus / workers / delay_ms / loss_pct when listed}, \"safety_reason\": str (one sentence: why these "
        "limits and this strength)}")
    allowed = {c["fault"] for c in pool}
    data, source = _ask(llm, instruction, payload, lambda d: check_decision(d, allowed) or grounding_errors(
        {"r": [d["reason"], d["safety_reason"]]}, ["r"], {**payload, "chosen": d.get("settings")}))
    plan_next = remaining_plan[0]["fault"] if remaining_plan else None
    if data is None:
        if fallback is None:
            return None
        spec = CATALOG[fallback["fault"]]
        decision = {"fault": fallback["fault"], "reason": fallback["reason"], "source": "fallback",
                    "safety_reason": "default strength and the configured safety limits (no valid AI decision)",
                    **decide_settings(spec, None, caps, ctx.settings.max_fault_seconds, "default",
                                      ctx.settings.timings.fault_window_s)}
    else:
        spec = CATALOG[data["fault"]]
        decision = {"fault": data["fault"], "reason": data["reason"].strip(), "source": source,
                    "safety_reason": data["safety_reason"].strip(),
                    **decide_settings(spec, data.get("settings"), caps, ctx.settings.max_fault_seconds, source,
                                      ctx.settings.timings.fault_window_s)}
    decision.update(step=len(done) + 1, followed_plan=decision["fault"] == plan_next, plan_next=plan_next)
    return decision


def operator_decision(spec: FaultSpec, caps: Thresholds, max_fault_s: float, step: int,
                      window_s: float = 35.0) -> dict:
    """Settings for an experiment the operator chose (no AI): defaults within the same bounds."""
    return {"fault": spec.id, "reason": "chosen by the operator", "source": "operator",
            "safety_reason": "default strength and the configured safety limits", "step": step,
            "followed_plan": True, "plan_next": spec.id,
            **decide_settings(spec, None, caps, max_fault_s, "operator", window_s)}


def decision_record(decision: dict) -> dict:
    """JSON-safe copy of a decision (the Thresholds object becomes its description)."""
    return {k: v for k, v in decision.items() if k != "thresholds"}
