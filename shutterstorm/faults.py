"""Fault catalog and the generic undo machinery.

Every fault ShutterStorm applies is described by a
JSON *recipe* that says how to undo it, including the original values. Recipes
are self-contained, so the guard process and ``panic`` can undo them without
knowing which fault created them.

Flow for a fault::

    recipe = build_recipe(...)            # capture originals
    journal.add(recipe)                   # write-ahead
    spawn guard + start TTL watchdog
    apply                                 # docker pause / stop / ...
    ... observe ...
    undo_recipe(); verify_recipe()        # then journal.remove()
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import ML, POSTGRES, REDIS, SERVER
from .dockerctl import DockerError

NETEM_IMAGE = "nicolaka/netshoot"
NETEM_HELPER = "shutterstorm_netem_ml"
NETEM_ARGS = {"latency": ["delay", "3000ms"], "loss": ["loss", "30%"]}
LOAD_WORKERS = 24

# Fault strength the AI may choose per experiment: knob per fault kind, its default and hard bounds.
# Anything outside the bounds is clamped by planner.decide_settings before it reaches Docker.
FAULT_KNOBS = {
    "cpu": ("cpus", 0.25, 0.1, 1.0),            # Docker CPU limit (fraction of one CPU)
    "load": ("workers", LOAD_WORKERS, 8, 48),    # concurrent read-only clients
    "latency": ("delay_ms", 3000, 500, 5000),    # added network delay
    "loss": ("loss_pct", 30, 5, 50),             # dropped packets
}


def knob(params: dict | None, kind: str):
    """The value of ``kind``'s knob in ``params``, or its default."""
    name, default, _, _ = FAULT_KNOBS[kind]
    return (params or {}).get(name, default)


def netem_args(kind: str, params: dict | None) -> list[str]:
    if kind == "latency":
        return ["delay", f"{int(knob(params, kind))}ms"]
    return ["loss", f"{int(knob(params, kind))}%"]

SERVICE_OF = {SERVER: "server", ML: "ml", REDIS: "redis", POSTGRES: "database"}
COMPOSE_SERVICE_OF = {SERVER: "immich-server", ML: "immich-machine-learning", REDIS: "redis", POSTGRES: "database"}


@dataclass(frozen=True)
class FaultSpec:
    """One entry of the fault catalog."""

    id: str
    container: str
    kind: str          # pause | stop | restart | cpu | latency | loss | load | combo
    description: str
    likelihood: float  # used by the severity formula
    optional: bool = False  # never picked by the planner (needs an extra image, or riskier)
    parts: tuple[str, ...] = ()  # combo faults: the atomic faults applied together

    @property
    def service(self) -> str:
        """The service this experiment is about (for a combo: the non-load part)."""
        return SERVICE_OF[self.container]

    @property
    def services(self) -> tuple[str, ...]:
        """Every service the fault touches (one, or several for a combo)."""
        if not self.parts:
            return (self.service,)
        return tuple(dict.fromkeys(CATALOG[p].service for p in self.parts))

    @property
    def atomic(self) -> tuple["FaultSpec", ...]:
        """The atomic faults to apply: itself, or the parts of a combo."""
        return tuple(CATALOG[p] for p in self.parts) if self.parts else (self,)


CATALOG: dict[str, FaultSpec] = {f.id: f for f in [
    FaultSpec("ml_pause", ML, "pause", "Freeze the ML container (hung dependency)", 0.5),
    FaultSpec("ml_stop", ML, "stop", "Stop the ML container (dependency down)", 0.5),
    FaultSpec("redis_pause", REDIS, "pause", "Freeze Redis/Valkey (job queue hung)", 0.5),
    FaultSpec("redis_stop", REDIS, "stop", "Stop Redis/Valkey (job queue down)", 0.5),
    FaultSpec("db_pause", POSTGRES, "pause", "Freeze Postgres (database hung)", 0.5),
    FaultSpec("db_stop", POSTGRES, "stop", "Stop Postgres gracefully (database down)", 0.5, optional=True),
    FaultSpec("db_cpu_throttle", POSTGRES, "cpu", "Limit Postgres CPU (overloaded database; default 0.25 CPU)", 0.6),
    FaultSpec("server_restart", SERVER, "restart", "Restart the API server (container crash)", 0.8),
    FaultSpec("ml_cpu_throttle", ML, "cpu", "Limit ML CPU (overloaded dependency; default 0.25 CPU)", 0.6),
    FaultSpec("traffic_spike", SERVER, "load",
              f"Sudden traffic spike: concurrent read-only clients (default {LOAD_WORKERS})", 0.7),
    FaultSpec("ml_latency", ML, "latency", "Add network delay to ML (netem; default 3000 ms)", 0.6, optional=True),
    FaultSpec("ml_packet_loss", ML, "loss", "Drop ML network packets (lossy network, netem; default 30%)", 0.6,
              optional=True),
    # Combined failures: several atomic faults at once, each with its own journal entry and TTL.
    FaultSpec("spike_ml_down", ML, "combo", "Traffic spike while the ML service is down", 0.4,
              parts=("traffic_spike", "ml_stop")),
    FaultSpec("spike_redis_hung", REDIS, "combo", "Traffic spike while Redis is frozen", 0.4,
              parts=("traffic_spike", "redis_pause")),
]}
# Used with `run --no-plan` (the AI planner chooses otherwise).
DEFAULT_EXPERIMENTS = ["ml_pause", "ml_stop", "redis_pause", "redis_stop", "db_pause", "server_restart"]
ATOMIC_FAULTS = [f for f in CATALOG if not CATALOG[f].parts]


class FaultError(RuntimeError):
    pass


def get_fault(fault_id: str) -> FaultSpec:
    """Look up a fault; anything not in the catalog is rejected."""
    if fault_id not in CATALOG:
        raise FaultError(f"unknown fault {fault_id!r}. Allowed: {', '.join(CATALOG)}")
    return CATALOG[fault_id]


# ============================================================== recipes
def build_recipe(ctx, spec: FaultSpec) -> dict:
    """Describe how to undo ``spec`` (captures original values)."""
    c = spec.container
    if spec.kind == "pause":
        return {"type": "unpause", "container": c}
    if spec.kind == "stop":
        return {"type": "start", "container": c}
    if spec.kind == "restart":
        return {"type": "ensure_running", "container": c}
    if spec.kind == "cpu":
        return {"type": "cpu_quota", "container": c, "cpu_quota": ctx.docker.state(c)["cpu_quota"]}
    if spec.kind in NETEM_ARGS:
        return {"type": "netem", "container": c, "helper": NETEM_HELPER, "image": NETEM_IMAGE}
    if spec.kind == "load":
        # The load generator is a thread of the owning process and dies with it, so the
        # guard and panic have nothing to undo; in-process, undo stops the thread.
        return {"type": "stop_load", "container": c, "load_id": uuid.uuid4().hex[:8]}
    raise FaultError(f"no recipe for kind {spec.kind}")


def apply_fault_action(ctx, spec: FaultSpec, recipe: dict, params: dict | None = None) -> threading.Thread | None:
    """Perform the fault. ``restart`` runs in a background thread so probes can watch it."""
    d = ctx.docker
    c = spec.container
    if spec.kind == "pause":
        d.pause(c)
    elif spec.kind == "stop":
        d.stop(c)
    elif spec.kind == "restart":
        thread = threading.Thread(target=d.restart, args=(c,), daemon=True)
        thread.start()
        return thread
    elif spec.kind == "cpu":
        d.set_cpu_quota(c, int(float(knob(params, "cpu")) * 100_000))
    elif spec.kind in NETEM_ARGS:
        if not d.image_present(NETEM_IMAGE):
            raise FaultError(f"{spec.id} needs the {NETEM_IMAGE} image: run `docker pull {NETEM_IMAGE}` first")
        d.run_helper(NETEM_HELPER, NETEM_IMAGE, c, ["sleep", "600"])
        d.exec(NETEM_HELPER, ["tc", "qdisc", "add", "dev", "eth0", "root", "netem", *netem_args(spec.kind, params)])
    elif spec.kind == "load":
        from .load import start_load  # local import avoids a cycle

        start_load(ctx, recipe["load_id"], int(knob(params, "load")))
    elif spec.kind == "combo":
        raise FaultError("combo faults are applied part by part with apply_faults()")
    return None


def undo_recipe(ctx, recipe: dict) -> None:
    """Undo one recipe. Every branch is idempotent (safe to run twice)."""
    d = ctx.docker
    kind = recipe["type"]
    c = recipe.get("container")
    t = ctx.settings.timings
    if kind == "unpause":
        if d.state(c)["paused"]:
            d.unpause(c)
    elif kind in ("start", "ensure_running"):
        st = d.state(c)
        if st["paused"]:
            d.unpause(c)
        if not st["running"]:
            d.start(c)
        d.wait_healthy(c, t.healthy_timeout_s, t.poll_s)
    elif kind == "cpu_quota":
        d.set_cpu_quota(c, int(recipe["cpu_quota"]))
    elif kind == "set_cpus":  # journal entries written by older versions
        _restore_cpus(ctx, c, int(recipe["nano_cpus"]))
    elif kind == "netem":
        _remove_netem(ctx, recipe)
    elif kind == "stop_load":
        from .load import stop_load

        stop_load(recipe["load_id"])
    else:
        raise FaultError(f"unknown recipe type {kind}")


def verify_recipe(ctx, recipe: dict) -> tuple[bool, str]:
    """Check that the undo really happened."""
    d = ctx.docker
    kind = recipe["type"]
    c = recipe.get("container")
    if kind == "unpause":
        st = d.state(c)
        return (not st["paused"] and st["running"]), f"paused={st['paused']} running={st['running']}"
    if kind in ("start", "ensure_running"):
        ok = d.wait_healthy(c, ctx.settings.timings.healthy_timeout_s, ctx.settings.timings.poll_s)
        st = d.state(c)
        return ok, f"running={st['running']} health={st['health']}"
    if kind == "cpu_quota":
        now = d.state(c)["cpu_quota"]
        return now == int(recipe["cpu_quota"]), f"CpuQuota={now or 'unlimited'} expected={recipe['cpu_quota'] or 'unlimited'}"
    if kind == "set_cpus":
        now = d.state(c)["nano_cpus"] or 0
        return now == int(recipe["nano_cpus"]), f"NanoCpus={now} expected={recipe['nano_cpus']}"
    if kind == "netem":
        if d.list_helpers():
            return False, f"helpers still present: {d.list_helpers()}"
        has = netem_present(ctx)
        return has is not True, "no netem qdisc" if has is False else ("netem check skipped" if has is None else "netem still active")
    if kind == "stop_load":
        from .load import load_running

        running = load_running(recipe["load_id"])
        return not running, "load generator still running" if running else "load generator stopped"
    return False, f"unknown recipe type {kind}"


def undo_entry(ctx, entry: dict, who: str = "main") -> tuple[bool, str]:
    """Undo + verify a journal entry; remove it from the journal only if verified."""
    recipe = entry["recipe"]
    try:
        undo_recipe(ctx, recipe)
        ok, detail = verify_recipe(ctx, recipe)
    except Exception as exc:  # noqa: BLE001 - must report, never crash the safety path
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"
    if ok:
        ctx.journal.remove(entry["id"])
    ctx.log(f"[{who}] undo {entry['label']} ({recipe['type']}): {'verified' if ok else 'NOT verified'} - {detail}")
    return ok, detail


# ============================================================== helpers
def _restore_cpus(ctx, container: str, nano_cpus: int) -> None:
    """Legacy NanoCpus undo (journals of older versions). Needs a compose recreate on Docker 29.8."""
    if os.environ.get("SHUTTERSTORM_IN_CONTAINER") == "1":
        # Inside a container, compose would resolve Immich's relative volume paths against the
        # container's mount point and recreate the service with wrong paths. Never do that here.
        raise FaultError(f"cannot remove a --cpus limit on {container} from inside a container; "
                         "run `python -m shutterstorm panic` on the host")
    try:
        ctx.docker.set_nano_cpus(container, nano_cpus)
    except DockerError as exc:
        if nano_cpus != 0:
            raise
        # Some Docker versions reject --cpus 0; recreating the service drops the limit.
        ctx.log(f"--cpus 0 rejected ({exc}); recreating the service to remove the CPU limit")
        ctx.docker.compose(Path(ctx.settings.immich_dir), ["docker-compose.yml"],
                           ["up", "-d", "--force-recreate", COMPOSE_SERVICE_OF[container]])
    if ctx.docker.state(container)["nano_cpus"] != nano_cpus:
        ctx.docker.compose(Path(ctx.settings.immich_dir), ["docker-compose.yml"],
                           ["up", "-d", "--force-recreate", COMPOSE_SERVICE_OF[container]])
    ctx.docker.wait_healthy(container, ctx.settings.timings.healthy_timeout_s, ctx.settings.timings.poll_s)


def _remove_netem(ctx, recipe: dict) -> None:
    d = ctx.docker
    helper = recipe["helper"]
    if helper not in d.list_helpers():
        # The qdisc lives in the ML network namespace and outlives the helper.
        if not d.image_present(recipe["image"]):
            return
        d.run_helper(helper, recipe["image"], recipe["container"], ["sleep", "60"])
    d.exec(helper, ["tc", "qdisc", "del", "dev", "eth0", "root"], check=False)
    d.remove_helper(helper)


def netem_present(ctx) -> bool | None:
    """True/False if we could check the ML qdisc; None if the netshoot image is absent."""
    d = ctx.docker
    if not d.image_present(NETEM_IMAGE) or not d.state(ML)["running"]:
        return None
    probe = "shutterstorm_netem_check"
    d.run_helper(probe, NETEM_IMAGE, ML, ["sleep", "30"])
    try:
        out = d.exec(probe, ["tc", "qdisc", "show", "dev", "eth0"], check=False).stdout
        return "netem" in out
    finally:
        d.remove_helper(probe)


# ============================================================== active fault
@dataclass
class ActiveFault:
    """A fault that is currently applied, with its TTL watchdog."""

    spec: FaultSpec
    entry_id: str
    recipe: dict
    applied_at: float
    ttl_s: float
    thread: threading.Thread | None = None
    ttl_fired: bool = False
    undone: bool = False
    undo_detail: str = ""
    _timer: threading.Timer | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def apply_fault(ctx, spec: FaultSpec, ttl_s: float | None = None, params: dict | None = None) -> ActiveFault:
    """Journal, start the guard and TTL watchdog, then apply the fault."""
    from .guard import spawn_guard  # local import avoids a cycle

    ttl = min(ttl_s or ctx.settings.max_fault_seconds, ctx.settings.max_fault_seconds)
    ctx.docker.ensure_in_scope(spec.container)
    recipe = build_recipe(ctx, spec)
    entry_id = ctx.journal.add("fault", spec.id, recipe, ttl_s=ttl)
    spawn_guard(ctx)
    active = ActiveFault(spec, entry_id, recipe, time.time(), ttl)
    active._timer = threading.Timer(ttl, _ttl_expired, args=(ctx, active))
    active._timer.daemon = True
    active._timer.start()
    strength = f", {FAULT_KNOBS[spec.kind][0]}={knob(params, spec.kind)}" if spec.kind in FAULT_KNOBS else ""
    ctx.log(f"APPLY {spec.id}: {spec.description} (TTL {ttl:.0f}s{strength}, journal entry {entry_id})")
    try:
        active.thread = apply_fault_action(ctx, spec, recipe, params)
    except Exception:
        ctx.log(f"apply of {spec.id} failed; undoing immediately")
        undo_fault(ctx, active)
        raise
    return active


def apply_faults(ctx, spec: FaultSpec, ttl_s: float | None = None, params: dict | None = None) -> list[ActiveFault]:
    """Apply a fault, or every part of a combo. If a part fails, the parts already applied are undone."""
    applied: list[ActiveFault] = []
    try:
        for part in spec.atomic:
            applied.append(apply_fault(ctx, part, ttl_s=ttl_s, params=params))
    except Exception:
        undo_faults(ctx, applied)
        raise
    return applied


def undo_faults(ctx, actives: list[ActiveFault], who: str = "main") -> tuple[bool, str]:
    """Undo several active faults (load first, then reverse order); ok only if every undo was verified."""
    order = sorted(reversed(actives), key=lambda a: a.spec.kind != "load")
    results = [(a.spec.id, *undo_fault(ctx, a, who=who)) for a in order]
    return all(ok for _, ok, _ in results), "; ".join(f"{fid}: {detail}" for fid, _, detail in results)


def _ttl_expired(ctx, active: ActiveFault) -> None:
    if active.undone:
        return
    active.ttl_fired = True
    ctx.log(f"TTL of {active.ttl_s:.0f}s reached for {active.spec.id}: watchdog undoing it")
    undo_fault(ctx, active, who="ttl-watchdog")


def undo_fault(ctx, active: ActiveFault, who: str = "main") -> tuple[bool, str]:
    """Undo an active fault once (thread-safe; later calls return the first result)."""
    with active._lock:
        if active.undone:
            return True, active.undo_detail
        if active._timer:
            active._timer.cancel()
        if active.thread is not None:
            active.thread.join(timeout=180)
        entry = ctx.journal.get(active.entry_id)
        if entry is None:  # someone else (guard/panic) already undid it
            ok, detail = verify_recipe(ctx, active.recipe)
            ctx.log(f"[{who}] {active.spec.id} already undone elsewhere; verify: {detail}")
        else:
            ok, detail = undo_entry(ctx, entry, who=who)
        active.undone = ok
        active.undo_detail = detail
        return ok, detail
