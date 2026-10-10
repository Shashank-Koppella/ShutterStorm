"""Before/after comparison of two runs (stretch goal: re-run experiments after a fix).

ShutterStorm never applies a fix. The intended loop is:

1. ``run``                                  -> report with a suggested fix
2. a human reviews and applies the fix by hand (e.g. the manual steps in the report)
3. ``run --experiments <same faults> --compare-to <run 1>``  (or ``compare --before A --after B``)

The comparison only uses measured data (scores, heatmaps, recovery); the verdict per
fault is a fixed rule, not an LLM opinion.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings

IMPROVED_DELTA = 5.0
BAD_CELLS = ("slow", "fast-fail", "hung")


def resolve_run(settings: Settings, run: str) -> Path:
    path = Path(run)
    if not path.exists():
        path = settings.runs_dir / run
    if not (path / "findings.json").exists():
        raise FileNotFoundError(f"no findings.json in {path}")
    return path


def load_findings(settings: Settings, run: str) -> dict:
    return json.loads((resolve_run(settings, run) / "findings.json").read_text(encoding="utf-8"))


def _verdict(delta: float, broken_before: list[str], broken_after: list[str]) -> str:
    hung_fixed = len(broken_after) < len(broken_before)
    if delta >= IMPROVED_DELTA or (hung_fixed and delta >= 0):
        return "improved"
    if delta <= -IMPROVED_DELTA or len(broken_after) > len(broken_before):
        return "regressed"
    return "no clear change"


def compare_runs(settings: Settings, before_run: str, after: dict) -> dict:
    """Compare the experiments of ``after`` (findings dict) with the same faults in ``before_run``."""
    before = load_findings(settings, before_run)
    if bool(before.get("simulated")) != bool(after.get("simulated")):
        raise ValueError("cannot compare a simulated run with a real one")
    old = {e["fault"]: e for e in before.get("experiments", [])}
    rows = []
    for e in after.get("experiments", []):
        b = old.get(e["fault"])
        if not b:
            continue
        broken_b = [j for j, c in b["heatmap"].items() if c in BAD_CELLS]
        broken_a = [j for j, c in e["heatmap"].items() if c in BAD_CELLS]
        delta = round(e["scoring"]["score"] - b["scoring"]["score"], 1)
        rows.append({
            "fault": e["fault"],
            "score_before": b["scoring"]["score"], "score_after": e["scoring"]["score"], "delta": delta,
            "severity_before": b["scoring"]["severity_label"], "severity_after": e["scoring"]["severity_label"],
            "recovery_before_s": b.get("recovery_s"), "recovery_after_s": e.get("recovery_s"),
            "heatmap_before": b["heatmap"], "heatmap_after": e["heatmap"],
            "broken_before": broken_b, "broken_after": broken_a,
            "verdict": _verdict(delta, broken_b, broken_a),
        })
    return {
        "before_run": before["run_id"], "after_run": after["run_id"],
        "overall_before": before.get("overall_score"), "overall_after": after.get("overall_score"),
        "rows": rows,
        "unmatched": sorted({e["fault"] for e in after.get("experiments", [])} - set(old)),
        "rule": f"improved = score +{IMPROVED_DELTA:g} or fewer broken journeys; regressed = score "
                f"-{IMPROVED_DELTA:g} or more broken journeys. Changes between the runs were made by a human.",
    }


def cmd_compare(settings: Settings, before: str, after: str) -> dict:
    """Attach a comparison to the *after* run's findings and re-render its report."""
    from .report import render_reports

    after_dir = resolve_run(settings, after)
    data = json.loads((after_dir / "findings.json").read_text(encoding="utf-8"))
    data["comparison"] = compare_runs(settings, before, data)
    (after_dir / "findings.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    render_reports(after_dir)
    return data["comparison"]
