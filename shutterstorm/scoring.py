"""Resilience scoring (pure functions, no I/O).

Per experiment, six components in [0, 1]:

A availability  fraction of probe calls that succeeded during the fault
L latency       min(1, baseline_p95 / fault_p95), averaged over journeys with successes
C containment   fraction of journeys *not* depending on the faulted service that stayed healthy
I integrity     uploads made during the fault eventually processed with a matching SHA-1
R recovery      1 if everything recovered within 30 s of the undo, linear to 0 at 300 s
B bounded       fraction of failed calls that failed fast (< 5 s) instead of hanging

S_e = 100 * (0.30A + 0.15L + 0.20C + 0.15I + 0.10R + 0.10B)
severity = (100 - S_e) / 100 * criticality(service) * likelihood(fault)

The ranking of findings is decided by this math; the LLM only explains it.
"""
from __future__ import annotations

from .probes import FAST_FAIL_SECONDS, percentile

WEIGHTS = {"A": 0.30, "L": 0.15, "C": 0.20, "I": 0.15, "R": 0.10, "B": 0.10}
CRITICALITY = {"database": 1.0, "server": 1.0, "redis": 0.8, "ml": 0.6}
LIKELIHOOD = {"pause": 0.5, "stop": 0.5, "restart": 0.8, "cpu": 0.6, "latency": 0.6}
RECOVERY_FULL_S = 30.0
RECOVERY_ZERO_S = 300.0


def _p95_by_journey(results: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for j in {r["journey"] for r in results}:
        p = percentile([r["latency_ms"] for r in results if r["journey"] == j and r["ok"]], 95)
        if p is not None:
            out[j] = p
    return out


def availability(fault_results: list[dict]) -> float:
    return sum(r["ok"] for r in fault_results) / len(fault_results) if fault_results else 1.0


def latency_component(baseline: list[dict], fault_results: list[dict]) -> float:
    base, fault = _p95_by_journey(baseline), _p95_by_journey(fault_results)
    ratios = [min(1.0, base[j] / fault[j]) for j in fault if j in base and fault[j] > 0]
    if not ratios:
        return 0.0 if fault_results else 1.0
    return sum(ratios) / len(ratios)


def containment(fault_results: list[dict], service: str, journey_deps: dict[str, list[str]]) -> float:
    independent = [j for j, deps in journey_deps.items() if service not in deps]
    seen = [j for j in independent if any(r["journey"] == j for r in fault_results)]
    if not seen:
        return 1.0
    healthy = [j for j in seen if all(r["ok"] for r in fault_results if r["journey"] == j)]
    return len(healthy) / len(seen)


def recovery_component(recovery_s: float | None) -> float:
    if recovery_s is None:
        return 0.0
    if recovery_s <= RECOVERY_FULL_S:
        return 1.0
    return max(0.0, 1 - (recovery_s - RECOVERY_FULL_S) / (RECOVERY_ZERO_S - RECOVERY_FULL_S))


def bounded_failure(fault_results: list[dict]) -> float:
    failed = [r for r in fault_results if not r["ok"]]
    if not failed:
        return 1.0
    fast = [r for r in failed if not r["timed_out"] and r["latency_ms"] < FAST_FAIL_SECONDS * 1000]
    return len(fast) / len(failed)


def score_experiment(evidence: dict, baseline: list[dict], journey_deps: dict[str, list[str]],
                     kind: str) -> dict:
    """Compute the components, S_e and severity for one experiment."""
    fault = evidence["fault_results"]
    comps = {
        "A": availability(fault),
        "L": latency_component(baseline, fault),
        "C": containment(fault, evidence["service"], journey_deps),
        "I": evidence["integrity"]["score"],
        "R": recovery_component(evidence["recovery_s"]),
        "B": bounded_failure(fault),
    }
    s_e = 100 * sum(WEIGHTS[k] * v for k, v in comps.items())
    crit = CRITICALITY[evidence["service"]]
    like = LIKELIHOOD[kind]
    severity = (100 - s_e) / 100 * crit * like
    return {
        "components": {k: round(v, 3) for k, v in comps.items()},
        "score": round(s_e, 1),
        "criticality": crit,
        "likelihood": like,
        "severity": round(severity, 3),
        "severity_label": severity_label(severity),
    }


def severity_label(severity: float) -> str:
    """Bucket the numeric severity for humans."""
    # Thresholds fit the real range: max is ~0.8 (DB/server, score 0, likelihood 0.8);
    # a single hung journey on ML lands around 0.04-0.07.
    if severity >= 0.20:
        return "critical"
    if severity >= 0.10:
        return "high"
    if severity >= 0.04:
        return "medium"
    return "low"


def overall_score(experiments: list[dict]) -> float | None:
    """Criticality-weighted mean of S_e over all experiments."""
    pairs = [(e["scoring"]["criticality"], e["scoring"]["score"]) for e in experiments if e.get("scoring")]
    total = sum(w for w, _ in pairs)
    return round(sum(w * s for w, s in pairs) / total, 1) if total else None


def classify_cell(results: list[dict], baseline: list[dict], journey: str) -> str:
    """Heatmap cell: ok | slow | fast-fail | hung | n/a."""
    rs = [r for r in results if r["journey"] == journey]
    if not rs:
        return "n/a"
    if any(r["timed_out"] for r in rs):
        return "hung"
    if any(not r["ok"] for r in rs):
        return "fast-fail"
    base = percentile([r["latency_ms"] for r in baseline if r["journey"] == journey and r["ok"]], 95)
    fault = percentile([r["latency_ms"] for r in rs], 95)
    if base is not None and fault is not None and fault > max(2 * base, base + 500):
        return "slow"
    return "ok"
