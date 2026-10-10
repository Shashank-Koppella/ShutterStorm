"""Kill switch (``panic``), the abort rule and the threshold safety monitor.

``panic`` is deliberately blunt: it undoes every journal entry, then does a
generic restore of the whole sandbox against the last checkpoint, whatever
state the system is in. It also writes the STOP file so a run that is still
going stops at its next step.
"""
from __future__ import annotations

from .checkpoint import load_last_checkpoint
from .config import CORE_CONTAINERS, Thresholds
from .faults import NETEM_HELPER, NETEM_IMAGE, FaultSpec, _remove_netem, _restore_cpus, undo_entry
from .load import stop_all as stop_all_load
from .probes import AssetTracker, percentile
from .topology import JOURNEY_DEPS


class AbortRun(RuntimeError):
    """Raised when the run must stop (abort rule, STOP file, failed undo)."""


def check_stop(ctx) -> None:
    """Called between workflow steps: honour the STOP file."""
    if ctx.journal.stop_requested():
        raise AbortRun(f"STOP file present ({ctx.journal.stop_path})")


def protected_journeys(spec: FaultSpec, journey_deps: dict[str, list[str]] = JOURNEY_DEPS) -> list[str]:
    """Journeys outside the fault's intended blast radius: they need none of the faulted services.

    A load part of a combo is ignored here (it targets the whole server by design); a fault on
    the server itself has no protected journeys, because every journey goes through it.
    """
    targets = {p.service for p in spec.atomic if p.kind != "load"} or set(spec.services)
    if "server" in targets:
        return []
    return [j for j, deps in journey_deps.items() if not targets & set(deps)]


class SafetyMonitor:
    """Checks every fault-window probe round against :class:`Thresholds`.

    ``check`` returns a breach description once a limit has been exceeded for
    ``thresholds.rounds`` consecutive rounds; the caller then rolls the fault back.
    """

    def __init__(self, thresholds: Thresholds, spec: FaultSpec) -> None:
        self.t = thresholds
        self.spec = spec
        self.protected = protected_journeys(spec) if thresholds.blast_radius else []
        # A restart is one action that cannot be rolled back: stopping early would only cut the
        # observation short. Its breaches are recorded; the outage is bounded by the restart itself
        # and by the liveness abort rule after the undo.
        self.can_roll_back = all(p.kind != "restart" for p in spec.atomic)
        self.streak = 0
        self.history: list[dict] = []

    def evaluate(self, results) -> list[str]:
        """Which limits this single round exceeds (no streak logic)."""
        if not results:
            return []
        reasons = []
        failed = [r for r in results if not r.ok]
        rate = len(failed) / len(results)
        if rate > self.t.max_error_rate:
            reasons.append(f"error rate {rate:.0%} > {self.t.max_error_rate:.0%}")
        p95 = percentile([r.latency_ms for r in results if r.ok], 95)
        if p95 is not None and p95 > self.t.max_p95_ms:
            reasons.append(f"p95 latency {p95:.0f} ms > {self.t.max_p95_ms:.0f} ms")
        outside = sorted({r.journey for r in failed if r.journey in self.protected})
        if outside:
            reasons.append(f"blast radius exceeded: {', '.join(outside)} failed although "
                           f"{'/'.join(self.spec.services)} is not one of their dependencies")
        self.history.append({"error_rate": round(rate, 3), "p95_ms": p95, "outside_blast_radius": outside,
                             "breached": reasons})
        return reasons

    def check(self, results) -> str | None:
        reasons = self.evaluate(results)
        self.streak = self.streak + 1 if reasons else 0
        if reasons and self.streak >= self.t.rounds and self.can_roll_back:
            return "; ".join(reasons) + f" (for {self.streak} consecutive round(s))"
        return None


def panic(ctx, reason: str = "manual") -> dict:
    """Undo everything and restore the sandbox. Returns a summary of what was done."""
    ctx.log(f"PANIC ({reason}): stopping run, undoing journal, restoring checkpoint")
    ctx.journal.request_stop()
    actions: list[str] = []
    if stop_all_load():
        actions.append("stopped in-process traffic generators")

    for entry in ctx.journal.entries():
        ok, detail = undo_entry(ctx, entry, who="panic")
        actions.append(f"journal {entry['label']}: {'ok' if ok else 'FAILED'} ({detail})")

    d = ctx.docker
    for name in CORE_CONTAINERS:
        st = d.state(name)
        if not st["exists"]:
            actions.append(f"{name}: missing (cannot restore without compose up)")
            continue
        if st["paused"]:
            d.unpause(name)
            actions.append(f"unpaused {name}")
        if not st["running"]:
            d.start(name)
            actions.append(f"started {name}")

    for helper in d.list_helpers():
        if helper == NETEM_HELPER:
            _remove_netem(ctx, {"helper": helper, "image": NETEM_IMAGE, "container": "immich_machine_learning"})
        else:
            d.remove_helper(helper)
        actions.append(f"removed helper {helper}")

    checkpoint = load_last_checkpoint(ctx)
    if checkpoint:
        for name in CORE_CONTAINERS:
            want_quota = checkpoint["containers"][name].get("cpu_quota", 0)
            if d.state(name)["cpu_quota"] != want_quota:
                d.set_cpu_quota(name, want_quota)
                actions.append(f"restored CpuQuota of {name} to {want_quota or 'unlimited'}")
            want = checkpoint["containers"][name]["nano_cpus"] or 0
            if (d.state(name)["nano_cpus"] or 0) != want:
                _restore_cpus(ctx, name, want)
                actions.append(f"restored NanoCpus of {name} to {want}")
        for name in CORE_CONTAINERS:
            d.wait_healthy(name, ctx.settings.timings.healthy_timeout_s, ctx.settings.timings.poll_s)
    else:
        actions.append("no checkpoint found: skipped CPU-limit restore")

    try:
        tracker = AssetTracker(ctx.journal.state_dir / "probe_assets.json")
        result = tracker.cleanup(ctx.admin(fresh=True))
        actions.append(f"deleted {result['deleted']} probe assets")
    except Exception as exc:  # noqa: BLE001
        actions.append(f"probe asset cleanup failed: {exc}")

    left = ctx.journal.entries()
    for line in actions:
        ctx.log(f"  panic: {line}")
    ctx.log(f"PANIC done; journal entries left: {len(left)}")
    return {"reason": reason, "actions": actions, "journal_left": len(left)}
