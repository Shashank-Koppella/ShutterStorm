"""Kill switch (``panic``) and the abort rule.

``panic`` is deliberately blunt: it undoes every journal entry, then does a
generic restore of the whole sandbox against the last checkpoint, whatever
state the system is in. It also writes the STOP file so a run that is still
going stops at its next step.
"""
from __future__ import annotations

from .checkpoint import load_last_checkpoint
from .config import CORE_CONTAINERS
from .faults import NETEM_HELPER, NETEM_IMAGE, _remove_netem, _restore_cpus, undo_entry
from .probes import AssetTracker


class AbortRun(RuntimeError):
    """Raised when the run must stop (abort rule, STOP file, failed undo)."""


def check_stop(ctx) -> None:
    """Called between workflow steps: honour the STOP file."""
    if ctx.journal.stop_requested():
        raise AbortRun(f"STOP file present ({ctx.journal.stop_path})")


def panic(ctx, reason: str = "manual") -> dict:
    """Undo everything and restore the sandbox. Returns a summary of what was done."""
    ctx.log(f"PANIC ({reason}): stopping run, undoing journal, restoring checkpoint")
    ctx.journal.request_stop()
    actions: list[str] = []

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
