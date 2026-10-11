"""One chaos experiment: inject -> observe (with the safety monitor) -> undo -> recovery -> integrity.

This is pure deterministic code (no LLM). It is used by ``inject`` (Day-1
demo) and by the full workflow. A combo fault applies all its parts together.
After every probe round the :class:`~shutterstorm.safety.SafetyMonitor` checks the
impact thresholds; a breach rolls the fault back at once (``auto_stopped``).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

from .config import SERVER, Thresholds, redact
from .events import round_results
from .context import write_json
from .faults import FaultSpec, apply_faults, undo_faults
from .load import load_stats
from .probes import Prober, ProbeResult, summarize
from .reporting_io import append_probes_csv
from .safety import AbortRun, SafetyMonitor, check_stop

OBSERVED = ("J0", "J1", "J2", "J3", "J4", "J5", "J6")
RECOVERY_JOURNEYS = ("J0", "J1", "J2", "J3", "J4")
QUEUE_SNAPSHOT_TIMEOUT_S = 3.0  # GET /queues hangs too when Postgres/Redis is frozen


def run_experiment(ctx, prober: Prober, spec: FaultSpec, run_dir: Path, label: str | None = None,
                   window_s: float | None = None, on_round=None, thresholds: Thresholds | None = None,
                   params: dict | None = None) -> dict:
    """Run one fault experiment and return all evidence as a dict."""
    t = ctx.settings.timings
    label = label or spec.id
    ev_dir = run_dir / "evidence" / label
    prober.experiment = label
    window = min(window_s or t.fault_window_s, ctx.settings.max_fault_seconds - 5)
    monitor = SafetyMonitor(thresholds or ctx.settings.thresholds, spec)
    check_stop(ctx)

    queues_before = prober.queue_snapshot()
    actives = apply_faults(ctx, spec, ttl_s=ctx.settings.max_fault_seconds, params=params)
    limits = thresholds or ctx.settings.thresholds
    ctx.event("injected", fault=label, parts=[p.id for p in spec.atomic], params=params or {}, window_s=window,
              limits={"max_error_rate": limits.max_error_rate, "max_p95_ms": limits.max_p95_ms},
              applied_at=round(time.time(), 3))
    fault_results: list[ProbeResult] = []
    queue_series: list[dict] = []
    logs: dict[str, str] = {}
    auto_stop: dict | None = None
    load: dict | None = None
    applied_at = time.time()
    try:
        time.sleep(t.settle_s)
        start = time.time()
        round_no = 0
        while time.time() - start < window and not any(a.ttl_fired for a in actives):
            check_stop(ctx)
            round_no += 1
            round_start = time.time()
            results = prober.run_round("fault", OBSERVED, async_deadline=t.fault_async_deadline_s)
            fault_results += results
            if on_round:
                on_round("fault", round_no, results)
            ctx.event("probe_round", fault=label, phase="fault", round=round_no,
                      t_rel=round(time.time() - applied_at, 2), results=round_results(results))
            breach = monitor.check(results)  # before the queue snapshot, which can itself hang
            if monitor.history:
                h = monitor.history[-1]
                ctx.event("safety_check", fault=label, round=round_no, error_rate=h["error_rate"], p95_ms=h["p95_ms"],
                          outside_blast_radius=h["outside_blast_radius"], breached=h["breached"],
                          limits={"max_error_rate": limits.max_error_rate, "max_p95_ms": limits.max_p95_ms},
                          can_roll_back=monitor.can_roll_back)
            queue_series.append({"t": round(time.time() - start, 1),
                                 "queues": prober.queue_snapshot(timeout=QUEUE_SNAPSHOT_TIMEOUT_S)})
            if breach:
                auto_stop = {"reason": breach, "round": round_no, "after_s": round(time.time() - applied_at, 1)}
                ctx.log(f"SAFETY THRESHOLD BREACHED for {spec.id}: {breach}. Rolling back now "
                        f"({auto_stop['after_s']}s after injection, window was {window:.0f}s)")
                ctx.event("rollback", fault=label, reason=breach, after_s=auto_stop["after_s"], window_s=window)
                break
            time.sleep(max(0.0, t.round_interval_s - (time.time() - round_start)))
        for name in dict.fromkeys(p.container for p in spec.atomic) | {SERVER: None}:
            logs[name] = redact(ctx.docker.logs(name, tail=80), ctx.settings.secrets())
    finally:
        undo_started = time.time()
        for a in actives:
            if a.recipe["type"] == "stop_load":
                load = load_stats(a.recipe["load_id"])
        undo_ok, undo_detail = undo_faults(ctx, actives)
        for a in actives:
            if a.recipe["type"] == "stop_load":
                load = load_stats(a.recipe["load_id"]) or load
    ctx.event("undone", fault=label, ok=undo_ok, detail=undo_detail, t_rel=round(undo_started - applied_at, 2))
    if not undo_ok:
        raise AbortRun(f"undo of {spec.id} could not be verified: {undo_detail}")

    recovery_results, recovery_s = wait_for_recovery(ctx, prober, undo_started, on_round, label, applied_at)
    ctx.event("recovered", fault=label, recovery_s=recovery_s)
    integrity = check_integrity(ctx, prober, label)
    queues_after = prober.queue_snapshot()

    evidence = {
        "label": label,
        "fault": spec.id,
        "container": spec.container,
        "service": spec.service,
        "services": list(spec.services),
        "parts": [p.id for p in spec.atomic],
        "params": params or {},
        "window_s": window,
        "fault_duration_s": round(undo_started - applied_at, 1),
        "applied_at": round(applied_at, 3),
        "undone_at": round(undo_started, 3),
        "ttl_fired": any(a.ttl_fired for a in actives),
        "auto_stopped": auto_stop,
        "safety": {"thresholds": (thresholds or ctx.settings.thresholds).describe(),
                   "protected_journeys": monitor.protected, "rounds": monitor.history},
        "load": load,
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


def wait_for_recovery(ctx, prober: Prober, undo_started: float, on_round=None, label: str = "",
                      applied_at: float | None = None) -> tuple[list[ProbeResult], float | None]:
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
        ctx.event("probe_round", fault=label, phase="recovery", round=round_no,
                  t_rel=round(time.time() - (applied_at or undo_started), 2), results=round_results(r))
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
