"""Small helpers to save probe results and print summary tables."""
from __future__ import annotations

import csv
from pathlib import Path

from .probes import JOURNEYS, ProbeResult, summarize

CSV_FIELDS = ["t", "experiment", "phase", "journey", "ok", "status_code", "latency_ms", "timed_out", "error_text"]


def append_probes_csv(path: Path, results: list[ProbeResult]) -> None:
    """Append probe results to ``probes.csv`` (header written once)."""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        for r in results:
            row = r.to_dict()
            writer.writerow({k: row.get(k) for k in CSV_FIELDS})


def summary_table(results: list[ProbeResult]) -> str:
    """A plain-text per-journey table."""
    lines = [f"{'journey':<34} {'ok':>6} {'hung':>5} {'fail':>5} {'p50 ms':>8} {'p95 ms':>8}"]
    for journey, s in summarize(results).items():
        p50 = f"{s['p50_ms']:.0f}" if s["p50_ms"] is not None else "-"
        p95 = f"{s['p95_ms']:.0f}" if s["p95_ms"] is not None else "-"
        lines.append(f"{journey + ' ' + JOURNEYS[journey]:<34} {s['ok']:>3}/{s['calls']:<2} "
                     f"{s['hung']:>5} {s['fast_fail']:>5} {p50:>8} {p95:>8}")
    return "\n".join(lines)
