"""One chaos experiment: inject -> observe -> undo -> recovery -> integrity.

This is pure deterministic code (no LLM). It is used by ``inject`` (Day-1
demo) and by the full workflow, including the re-test inside the repair loop.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

from .config import SERVER, redact
from .context import write_json
from .faults import FaultSpec, apply_fault, undo_fault
from .probes import Prober, ProbeResult, summarize
from .reporting_io import append_probes_csv
from .safety import AbortRun, check_stop

OBSERVED = ("J0", "J1", "J2", "J3", "J4", "J5", "J6")
RECOVERY_JOURNEYS = ("J0", "J1", "J2", "J3", "J4")


def run_experiment(ctx, prober: Prober, spec: FaultSpec, run_dir: Path, label: str | None = None,
                   window_s: float | None = None, on_round=None) -> dict:
    """Run one fault experiment and return all evidence as a dict."""
    t = ctx.settings.timings
    label = label or spec.id
    ev_dir = run_dir / "evidence" / label
    prober.experiment = label
    window = min(window_s or t.fault_window_s, ctx.settings.max_fault_seconds - 5)
    check_stop(ctx)

    queues_before = prober.queue_snapshot()
    active = apply_fault(ctx, spec, ttl_s=ctx.settings.max_fault_seconds)
    fault_results: list[ProbeResult] = []
    queue_series: list[dict] = []
    logs: dict[str, str] = {}
    try:
        time.sleep(t.settle_s)
        start = time.time()
        round_no = 0
        while time.time() - start < window and not active.ttl_fired:
            check_stop(ctx)
            round_no += 1
            round_start = time.time()
            results = prober.run_round("fault", OBSERVED, async_deadline=t.fault_async_deadline_s)
            fault_results += results
            queue_series.append({"t": round(time.time() - start, 1), "queues": prober.queue_snapshot()})
            if on_round:
                on_round("fault", round_no, results)
            time.sleep(max(0.0, t.round_interval_s - (time.time() - round_start)))
        for name in {spec.container, SERVER}:
            logs[name] = redact(ctx.docker.logs(name, tail=80), ctx.settings.secrets())
    finally:
        undo_started = time.time()
        undo_ok, undo_detail = undo_fault(ctx, active)
    if not undo_ok:
        raise AbortRun(f"undo of {spec.id} could not be verified: {undo_detail}")

    recovery_results, recovery_s = wait_for_recovery(ctx, prober, undo_started, on_round)
    integrity = check_integrity(ctx, prober, label)
    queues_after = prober.queue_snapshot()

    evidence = {
        "label": label,
        "fault": spec.id,
        "container": spec.container,
        "service": spec.service,
        "window_s": window,
        "ttl_fired": active.ttl_fired,
        "undo": {"ok": undo_ok, "detail": undo_detail},
        "recovery_s": recovery_s,
        "fault_summary": summarize(fault_results),
        "recovery_summary": summarize(recovery_results),
        "integrity": integrity,
        "queues": {"before": queues_before, "during": queue_series, "after": queues_after},
        "fault_results": [r.to_dict() for r in fault_results],
        "recovery_results": [r.to_dict() for r in recovery_results],
    }
    ev_dir.mkdir(parents=True, exist_ok=True)
    for name, text in logs.items():
        (ev_dir / f"{name}.log").write_text(text, encoding="utf-8")
    evidence["log_tails"] = {name: text.splitlines()[-80:] for name, text in logs.items()}
    write_json(ev_dir / "evidence.json", evidence)
    append_probes_csv(run_dir / "probes.csv", fault_results + recovery_results)
    return evidence


def wait_for_recovery(ctx, prober: Prober, undo_started: float, on_round=None) -> tuple[list[ProbeResult], float | None]:
    """Probe until every journey passes again. Applies the abort rule on liveness."""
    t = ctx.settings.timings
    results: list[ProbeResult] = []
    liveness_failing_since: float | None = None
    round_no = 0
    while time.time() - undo_started < t.recovery_limit_s:
        round_no += 1
        r = prober.run_round("recovery", RECOVERY_JOURNEYS, async_deadline=t.fault_async_deadline_s)
        results += r
        if on_round:
            on_round("recovery", round_no, r)
        if all(x.ok for x in r):
            return results, round(time.time() - undo_started, 1)
        j0 = next(x for x in r if x.journey == "J0")
        if j0.ok:
            liveness_failing_since = None
        else:
            liveness_failing_since = liveness_failing_since or time.time()
            if time.time() - liveness_failing_since > t.abort_liveness_s:
                raise AbortRun(f"J0 liveness failing for more than {t.abort_liveness_s:.0f}s after undo")
        time.sleep(t.poll_s)
    return results, None


def check_integrity(ctx, prober: Prober, label: str) -> dict:
    """Did uploads made during the fault eventually get processed with intact bytes?"""
    t = ctx.settings.timings
    client = prober.client()
    prober.tracker.reconcile(client)
    uploads = [u for u in prober.uploads if u.experiment == label and u.phase == "fault"]
    details = []
    for u in uploads:
        asset_id = u.asset_id or next((i for i, f in prober.tracker.assets.items() if f == u.filename), None)
        item = {"filename": u.filename, "asset_id": asset_id, "response_received": bool(u.asset_id),
                "stored": bool(asset_id), "processed": False, "sha1_ok": False}
        if asset_id:
            item["processed"] = prober.j5_processing(asset_id, t.async_deadline_s, "integrity").ok
            try:
                resp = client.original(asset_id)
                item["sha1_ok"] = resp.status_code == 200 and hashlib.sha1(resp.content).hexdigest() == u.sha1
            except requests.RequestException:
                pass
        details.append(item)
    stored = [d for d in details if d["stored"]]
    intact = [d for d in stored if d["processed"] and d["sha1_ok"]]
    ghost = [d for d in stored if not d["response_received"]]
    return {
        "attempted": len(details),
        "stored": len(stored),
        "intact": len(intact),
        "ghost_uploads": len(ghost),  # stored although the client never got a response
        "score": (len(intact) / len(stored)) if stored else 1.0,
        "details": details,
    }
