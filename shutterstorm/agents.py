"""The three LLM agents (Analyst, Repair, Reporter) and their deterministic fallbacks.

The agents only *explain* and *propose*. They never decide whether a patch
passed, never rank findings (the scoring math does), and never run commands.
Every result is tagged with ``source``: ``live``, ``cache`` or ``fallback``.
"""
from __future__ import annotations

import json
import re

from .faults import CATALOG, FaultSpec
from .llm import LLMClient
from .probes import JOURNEYS, PROTECTED_JOURNEYS
from .repair import KNOBS, ML2_SERVICE, ML_URL_1, ML_URL_2

SYSTEM = ("You are part of ShutterStorm, a chaos-engineering tool testing a self-hosted Immich photo app "
          "(services: immich-server API, immich-machine-learning, Redis/Valkey BullMQ job queue, Postgres) "
          "in a private sandbox. Answer ONLY with one JSON object that matches the requested schema. "
          "Base every claim on the evidence given; say 'unknown' rather than guess.")


# ====================================================================== schema checks
def _as_text(value) -> str:
    """Small models sometimes return a list or object where we asked for prose; flatten it."""
    if isinstance(value, list):
        return "; ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_as_text(v)}" for k, v in value.items())
    return str(value)


def _need(data: dict, fields: dict[str, type]) -> list[str]:
    """Check required fields. Text fields given as list/object are flattened in place (lenient on
    prose, strict on structure: lists, patches and journey ids are never coerced)."""
    errors = []
    for name, typ in fields.items():
        if typ is str and name in data and isinstance(data[name], (list, dict, int, float)):
            data[name] = _as_text(data[name])
        if name not in data:
            errors.append(f"missing field {name}")
        elif not isinstance(data[name], typ):
            errors.append(f"field {name} must be {typ.__name__}")
    return errors


def check_prediction(d: dict) -> list[str]:
    errors = _need(d, {"predicted_impact": list, "reasoning": str})
    if not errors and any(j not in JOURNEYS for j in d["predicted_impact"]):
        errors.append(f"predicted_impact may only contain {list(JOURNEYS)}")
    return errors


def check_analysis(d: dict) -> list[str]:
    return _need(d, {"summary": str, "affected_journeys": list, "root_cause_hypothesis": str,
                     "user_visible_symptom": str, "severity_rationale": str})


def check_repair(d: dict) -> list[str]:
    if d.get("no_fix_possible") is True:
        return _need(d, {"recommendation": str})
    errors = _need(d, {"rationale": str, "patch": dict, "expected_effect": str})
    if not errors and set(d["patch"]) - {"system_config", "compose_override"}:
        errors.append("patch may only contain system_config and compose_override")
    return errors


def check_report(d: dict) -> list[str]:
    errors = _need(d, {"executive_summary": str, "ranked_findings": list, "recommendations": list})
    for f in d.get("ranked_findings", []) if not errors else []:
        if not isinstance(f, dict) or "id" not in f or "fix" not in f:
            errors.append("each ranked finding needs at least id and fix")
            break
    return errors


# ====================================================================== evidence trimming
def evidence_digest(exp: dict, max_log_lines: int = 40) -> dict:
    """Compact, secret-free view of an experiment for prompts."""
    during = exp["queues"]["during"]
    max_waiting = {}
    for snap in during:
        for q, s in snap["queues"].items():
            if isinstance(s, dict):
                max_waiting[q] = max(max_waiting.get(q, 0), s.get("waiting", 0) + s.get("active", 0))
    return {
        "fault": exp["fault"],
        "fault_description": CATALOG[exp["fault"]].description,
        "fault_window_s": exp["window_s"],
        "fault_summary": exp["fault_summary"],
        "recovery_s_after_undo": exp["recovery_s"],
        "integrity": {k: v for k, v in exp["integrity"].items() if k != "details"},
        "queue_max_waiting_or_active_during_fault": {k: v for k, v in max_waiting.items() if v},
        "queue_errors_during_fault": sorted({snap["queues"]["error"] for snap in during if "error" in snap["queues"]}),
        "sample_errors": sorted({r["error_text"][:160] for r in exp["fault_results"] if r["error_text"]})[:8],
        "log_tails": {name: lines[-max_log_lines:] for name, lines in exp.get("log_tails", {}).items()},
    }


def _ask(llm: LLMClient, instruction: str, payload: dict, check) -> tuple[dict | None, str]:
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": instruction + "\n\nEVIDENCE (JSON):\n" + json.dumps(payload, default=str)[:24000]}]
    return llm.complete_json(messages, check)


# ====================================================================== grounding (anti-hallucination)
NUMBER_RE = re.compile(r"(?<![A-Za-z_\-./:#])\d+(?:\.\d+)?")
SMALL_INT_OK = 10  # counts like "1 of 7 journeys" are not worth flagging


def evidence_numbers(payload) -> set[float]:
    """Every number that appears anywhere in the evidence (values and inside strings)."""
    found: set[float] = set()

    def walk(x) -> None:
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            found.add(float(x))
        elif isinstance(x, str):
            found.update(float(n) for n in NUMBER_RE.findall(x))
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(payload)
    return found


def _grounded_number(x: float, known: set[float]) -> bool:
    if x <= SMALL_INT_OK and x == int(x):
        return True
    for e in known:
        for cand in (e, e * 1000, e / 1000, e * 100):  # s<->ms, ratio<->percent
            if abs(cand - x) < 1e-9 or round(cand) == x or round(cand, 1) == x or round(cand, 2) == x:
                return True
    return False


def grounding_errors(data: dict, text_fields: list[str], payload, own_fault: str | None = None) -> list[str]:
    """Reject numbers not present in the evidence and mentions of faults that were not active."""
    known = evidence_numbers(payload)
    errors = []
    for field in text_fields:
        values = data.get(field)
        texts = values if isinstance(values, list) else [values]
        for text in texts:
            if isinstance(text, dict):
                text = " ".join(str(v) for v in text.values())
            if not isinstance(text, str):
                continue
            bad = sorted({n for n in NUMBER_RE.findall(text) if not _grounded_number(float(n), known)})
            if bad:
                errors.append(f"{field} cites numbers not in the evidence: {', '.join(bad[:6])}")
            if own_fault:
                others = sorted(f for f in CATALOG if f != own_fault and re.search(rf"\b{f}\b", text))
                if others:
                    errors.append(f"{field} mentions faults that were not active in this experiment: {others}")
    return errors


def _grounded(check, text_fields: list[str], payload, own_fault: str | None = None):
    """Combine a schema check with the grounding check."""
    return lambda d: check(d) or grounding_errors(d, text_fields, payload, own_fault)


# ====================================================================== Analyst
ROOT_CAUSES = {
    ("ml", "pause"): "The server calls the ML service over HTTP and waits without a short timeout. A frozen ML "
                     "process keeps its TCP socket open but never answers, so smart search hangs instead of failing.",
    ("ml", "stop"): "The ML hostname stops resolving / refuses connections, which the server detects at once and "
                    "turns into a fast 500 - but the web UI shows it as 'no results', which is misleading.",
    ("ml", "cpu"): "ML inference is CPU-bound; a CPU limit makes each embedding slower.",
    ("ml", "latency"): "Every ML call pays the injected network delay; there is no fast fallback.",
    ("redis", "pause"): "Uploads store the file and DB row, then enqueue BullMQ jobs in Redis. On a frozen Redis the "
                        "enqueue blocks with no timeout, so the HTTP response never arrives although the asset exists "
                        "(a 'ghost upload'); thumbnails wait until Redis returns.",
    ("redis", "stop"): "With Redis gone, BullMQ cannot enqueue jobs; the server's Redis client reconnects in the "
                       "background, so job-producing requests fail or wait while DB-only requests continue.",
    ("database", "pause"): "Every authenticated request needs Postgres. A frozen DB keeps connections open, so queries "
                           "wait on the connection pool with no statement timeout and requests hang.",
    ("database", "stop"): "Without Postgres every authenticated request fails; the server may itself restart.",
    ("server", "restart"): "All traffic goes through a single API server container; while it restarts there is no "
                           "second instance to serve requests.",
}


def predict(llm: LLMClient, spec: FaultSpec, topology: dict, deterministic: dict) -> dict:
    """Ask the Analyst which journeys will break *before* injecting. Falls back to the map."""
    payload = {"fault": spec.id, "description": spec.description, "target_service": spec.service,
               "journeys": JOURNEYS, "dependency_edges": topology["edges"],
               "journey_dependencies": topology.get("journey_deps")}
    data, source = _ask(llm, "Before this fault is injected, predict which user journeys will be impacted "
                        "(fail, hang or become clearly slower). Schema: {\"predicted_impact\": [journey ids], "
                        "\"reasoning\": str}", payload, check_prediction)
    if data is None:
        return {**deterministic, "reasoning": deterministic["reason"], "source": "fallback"}
    return {"predicted_impacted": sorted(set(data["predicted_impact"])), "reasoning": data["reasoning"],
            "deterministic": deterministic["predicted_impacted"], "source": source}


ANALYSIS_TEXT = ["summary", "root_cause_hypothesis", "user_visible_symptom", "severity_rationale"]


def analyze(llm: LLMClient, exp: dict, topology: dict) -> dict:
    """Explain one experiment. Output schema per the README."""
    payload = {"dependency_edges": [e for e in topology["edges"] if e["source"] != "empirical"],
               "journeys": JOURNEYS, "baseline_summary": exp["baseline_summary"],
               "heatmap_row": exp["heatmap"], "prediction": exp["prediction"], **evidence_digest(exp)}
    data, source = _ask(llm, f"Analyse this ONE chaos experiment: only the fault '{exp['fault']}' was active; no "
                        "other fault was running. Log tails are the last lines of each container log and may contain "
                        "older, unrelated lines; only use lines that match the fault window. Heatmap cells: ok, slow, "
                        "fast-fail (clear error), hung (no answer within the timeout). Schema: {\"predicted_impact\": "
                        "str (what you would have predicted before injection), \"summary\": str (2-3 sentences), "
                        "\"affected_journeys\": [ids], \"root_cause_hypothesis\": str, \"user_visible_symptom\": str "
                        "(what a user of the web app would notice, 1 sentence), \"severity_rationale\": str}. "
                        "Pay special attention to the difference between hung and fast failures. Only cite numbers "
                        "that appear in the evidence; do not name libraries or products that are not in the evidence.",
                        payload, _grounded(check_analysis, ANALYSIS_TEXT, payload, exp["fault"]))
    if data is None:
        return {**fallback_analysis(exp), "source": "fallback"}
    return {**data, "source": source}


def fallback_analysis(exp: dict) -> dict:
    spec = CATALOG[exp["fault"]]
    cells = exp["heatmap"]
    affected = [j for j, c in cells.items() if c not in ("ok", "n/a")]
    parts = [f"{j} {JOURNEYS[j]}: {cells[j]}" for j in affected]
    symptom = "; ".join(parts) if parts else "no user-visible impact measured"
    hung = [j for j in affected if cells[j] == "hung"]
    ghost = exp["integrity"].get("ghost_uploads", 0)
    summary = (f"Under {spec.id} ({spec.description.lower()}), {len(affected)} of {len(cells)} journeys were affected"
               f"{' and ' + ', '.join(hung) + ' hung without any error' if hung else ''}. "
               f"Recovery after undo: {exp['recovery_s']}s.")
    if ghost:
        summary += f" {ghost} upload(s) were stored although the client never got a response."
    return {
        "predicted_impact": ", ".join(exp["prediction"]["predicted_impacted"]),
        "summary": summary,
        "affected_journeys": affected,
        "root_cause_hypothesis": ROOT_CAUSES.get((spec.service, spec.kind), "unknown"),
        "user_visible_symptom": symptom,
        "severity_rationale": (f"score {exp['scoring']['score']}/100, criticality {exp['scoring']['criticality']} "
                               f"x likelihood {exp['scoring']['likelihood']} -> severity {exp['scoring']['severity']}"
                               + ("; hangs are worse than fast errors because users get no feedback" if hung else "")),
    }


# ====================================================================== Repair
def _ml_failover_patch(timeout: int, interval: int) -> dict:
    return {
        "compose_override": {"services": {ML2_SERVICE: {
            "environment": {"MACHINE_LEARNING_PRELOAD__CLIP__TEXTUAL": "ViT-B-32__openai"}}}},
        "system_config": {"machineLearning": {
            "urls": [ML_URL_1, ML_URL_2],
            "availabilityChecks": {"enabled": True, "timeout": timeout, "interval": interval}}},
    }


CODE_LEVEL_FIXES = {
    "redis": "No config knob bounds this. Code-level fix: put a timeout (e.g. 2-3 s) on BullMQ enqueue calls and "
             "return the upload response once the file+DB row are committed, queueing jobs asynchronously; show "
             "per-file processing status in the web uploader instead of 'Processed 0/1' forever.",
    "database": "Code-level fix: set a Postgres statement_timeout / pool acquire timeout in the server so requests "
                "fail fast with a clear 503, and expose DB health in /server/ping-style readiness checks.",
    "server": "Needs a second API server replica behind a load balancer (outside the allowed knobs), or client-side "
              "retry with backoff in the web app.",
    "ml": "Code-level fix: give the server's ML HTTP client a request timeout and surface 'smart search temporarily "
          "unavailable' instead of an empty result page.",
}


def fallback_repair(exp: dict, attempts: list[dict]) -> dict:
    """Lookup table: fault -> candidate patches in order; then give up honestly."""
    service = CATALOG[exp["fault"]].service
    candidates = []
    if service == "ml":
        candidates = [
            {"rationale": "Add a second ML instance and list both URLs so the server can fail over; a short "
                          "availability-check interval lets it notice the frozen instance quickly.",
             "patch": _ml_failover_patch(1000, 5000),
             "expected_effect": "smart search keeps working (served by ML-2) while ML-1 is paused/down"},
            {"rationale": "Previous attempt was not enough; detect the dead instance faster.",
             "patch": _ml_failover_patch(500, 2000),
             "expected_effect": "failover within ~2 s of the fault"},
        ]
    # Compare against what was *proposed* (the stored "patch" is normalised and would never match).
    tried = {json.dumps(a.get("proposed"), sort_keys=True) for a in attempts}
    for c in candidates:
        if json.dumps(c["patch"], sort_keys=True) not in tried:
            return c
    return {"no_fix_possible": True, "recommendation": CODE_LEVEL_FIXES[service]}


def propose_repair(llm: LLMClient, exp: dict, current_values: dict, attempts: list[dict]) -> dict:
    """Ask for a patch using only the allowed knobs; feed back previous attempts."""
    payload = {"finding": exp["analysis"], "fault": exp["fault"], "heatmap_row": exp["heatmap"],
               "scores": exp["scoring"], "allowed_knobs": KNOBS, "current_values": current_values,
               "previous_attempts": attempts, "protected_journeys": list(PROTECTED_JOURNEYS),
               "evidence": {k: v for k, v in evidence_digest(exp, 20).items() if k != "log_tails"}}
    data, source = _ask(llm, "Propose ONE configuration-level fix using ONLY the allowed knobs, or say that none is "
                        "possible. The same fault will be re-injected to test it. Schema: {\"rationale\": str, "
                        "\"patch\": {\"system_config\": {nested partial config}, \"compose_override\": {\"services\": "
                        "{...}}}, \"expected_effect\": str} OR {\"no_fix_possible\": true, \"recommendation\": str "
                        "(a concrete code-level fix)}. For the second ML service you only need to give "
                        "{\"services\": {\"immich-machine-learning-2\": {}}}; fixed fields are filled in. Example of a "
                        "valid answer: {\"rationale\": \"one sentence\", \"patch\": {\"system_config\": "
                        "{\"job\": {\"thumbnailGeneration\": {\"concurrency\": 2}}}}, \"expected_effect\": "
                        "\"one sentence\"}. rationale and expected_effect must be plain strings.",
                        payload, check_repair)
    if data is None:
        return {**fallback_repair(exp, attempts), "source": "fallback"}
    return {**data, "source": source}


# ====================================================================== Reporter
def write_report(llm: LLMClient, experiments: list[dict], overall: float | None, scorecard: dict,
                 manual_findings: list[dict]) -> dict:
    """Executive summary + per-finding text. Order and severity always come from the scoring math."""
    ranked = sorted(experiments, key=lambda e: e["scoring"]["severity"], reverse=True)
    items = [{"id": e["label"], "fault": e["fault"], "severity": e["scoring"]["severity_label"],
              "severity_value": e["scoring"]["severity"], "score": e["scoring"]["score"],
              "heatmap_row": e["heatmap"], "analysis": e["analysis"], "repair": e.get("repair", {}).get("status"),
              "recommendation": e.get("repair", {}).get("recommendation")} for e in ranked]
    payload = {"overall_resilience_score": overall, "findings_in_rank_order": items,
               "prediction_scorecard": scorecard, "manual_findings": manual_findings}
    data, source = _ask(llm, "Write the findings report. Keep the given order and severities exactly. Lead with the "
                        "pattern 'a hung dependency is handled much worse than a down one' if the evidence shows it. "
                        "Schema: {\"executive_summary\": str, \"ranked_findings\": [{\"id\": str, \"title\": str, "
                        "\"severity\": str, \"evidence\": str, \"fix\": str, \"status\": str}], "
                        "\"recommendations\": [str]}. Only cite numbers that appear in the evidence.", payload,
                        _grounded(check_report, ["executive_summary", "ranked_findings"], payload))
    if data is None:
        data, source = fallback_report(items, overall), "fallback"
    by_id = {f.get("id"): f for f in data["ranked_findings"] if isinstance(f, dict)}
    fallback_items = {f["id"]: f for f in fallback_report(items, overall)["ranked_findings"]}
    data["ranked_findings"] = [  # math decides order + severity; LLM text fills in the rest
        {**fallback_items[i["id"]], **{k: v for k, v in by_id.get(i["id"], {}).items() if isinstance(v, str)},
         "id": i["id"], "severity": i["severity"], "severity_value": i["severity_value"],
         "status": fallback_items[i["id"]]["status"]}
        for i in items]
    return {**data, "source": source}


def fallback_report(items: list[dict], overall: float | None) -> dict:
    hung = [i for i in items if "hung" in i["heatmap_row"].values()]
    fast = [i for i in items if "fast-fail" in i["heatmap_row"].values() and i not in hung]
    summary = f"Overall resilience score: {overall}/100 across {len(items)} experiments. "
    if hung:
        summary += (f"The dominant weakness: hung dependencies ({', '.join(i['fault'] for i in hung)}) make requests "
                    f"wait silently until a timeout, while dependencies that are cleanly down "
                    f"({', '.join(i['fault'] for i in fast) or 'none'}) fail fast. Users get no feedback in the hung case.")
    findings = []
    for i in items:
        status = {"verified": "fixed (verified patch)", "unresolved": "unresolved",
                  "no_fix_possible": "unresolved (code-level fix needed)"}.get(i["repair"] or "", "not attempted")
        findings.append({
            "id": i["id"], "title": f"{i['fault']}: {i['analysis']['user_visible_symptom'][:90]}",
            "severity": i["severity"], "evidence": i["analysis"]["summary"],
            "fix": i["recommendation"] or CODE_LEVEL_FIXES.get(CATALOG[i["fault"]].service, ""),
            "status": status,
        })
    recs = ["Add request timeouts on every server->dependency call (ML HTTP, BullMQ enqueue, DB pool) so hangs become "
            "fast, explicit errors.",
            "Run a second ML instance and list both URLs in machineLearning.urls with a short availability-check interval.",
            "Show real per-file processing status in the web uploader; never leave 'Processed 0/1' without an error.",
            "Distinguish 'search unavailable' from 'no results' in the UI."]
    return {"executive_summary": summary, "ranked_findings": findings, "recommendations": recs}
