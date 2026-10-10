"""Structured run events: ``runs/<id>/events.jsonl``, one JSON object per line.

The dashboard draws live views (journey tiles, latency strips, safety meters, AI decision
cards) only from these events, never from parsed log text. Each event has a sequence number
``seq`` (so a client can ask for "everything after N"), a wall-clock time ``t`` and a ``type``.

Event types (fields besides seq / t / type):

    run_started      run_id, kind, simulated, budget, caps
    step             name
    plan             source, strategy, items [{rank, fault, failure_class, reason}]
    baseline_round   round, results [{journey, outcome, latency_ms}]
    decision         the AI (or operator) decision for the next experiment
    experiment_started  fault, parts, params, duration_s, limits, predicted
    injected         fault, parts, applied_at
    probe_round      fault, phase (fault|recovery), round, t_rel, results [...]
    safety_check     fault, round, error_rate, p95_ms, limits, breached [...]
    rollback         fault, reason, after_s
    undone           fault, ok, detail, t_rel
    recovered        fault, recovery_s
    experiment_done  fault, score, severity, severity_label, heatmap, auto_stopped, recovery_s
    cleanup          ok, failed [names], checks
    run_finished     status, overall_score, error

Writing never raises: events are a view, the run must not fail because of them.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class EventLog:
    """Append-only JSONL event log for one run."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, type_: str, **data) -> None:
        if self.path is None:
            return
        with self._lock:
            self._seq += 1
            line = json.dumps({"seq": self._seq, "t": round(time.time(), 3), "type": type_, **data}, default=str)
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


NULL = EventLog(None)


def read_events(path: Path, after: int = 0, limit: int = 2000) -> list[dict]:
    """Events with ``seq > after`` (at most ``limit``). A partly written last line is skipped."""
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("seq", 0) > after:
                out.append(ev)
                if len(out) >= limit:
                    break
    return out


def round_results(results) -> list[dict]:
    """Compact probe results for events."""
    return [{"journey": r.journey, "outcome": r.outcome, "latency_ms": round(r.latency_ms, 1)} for r in results]
