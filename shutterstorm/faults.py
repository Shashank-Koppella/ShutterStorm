"""Fault catalog and the generic undo machinery.

Every change ShutterStorm makes (fault *or* repair patch) is described by a
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

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CORE_CONTAINERS, ML, ML2, OVERRIDE_FILE, POSTGRES, REDIS, SERVER
from .dockerctl import DockerError

NETEM_IMAGE = "nicolaka/netshoot"
NETEM_HELPER = "shutterstorm_netem_ml"

SERVICE_OF = {SERVER: "server", ML: "ml", REDIS: "redis", POSTGRES: "database"}
COMPOSE_SERVICE_OF = {SERVER: "immich-server", ML: "immich-machine-learning", REDIS: "redis", POSTGRES: "database"}


@dataclass(frozen=True)
class FaultSpec:
    """One entry of the fault catalog."""

    id: str
    container: str
    kind: str          # pause | stop | restart | cpu | latency
    description: str
    likelihood: float  # used by the severity formula
    optional: bool = False

    @property
    def service(self) -> str:
        return SERVICE_OF[self.container]


CATALOG: dict[str, FaultSpec] = {f.id: f for f in [
    FaultSpec("ml_pause", ML, "pause", "Freeze the ML container (hung dependency)", 0.5),
    FaultSpec("ml_stop", ML, "stop", "Stop the ML container (dependency down)", 0.5),
    FaultSpec("redis_pause", REDIS, "pause", "Freeze Redis/Valkey (job queue hung)", 0.5),
    FaultSpec("redis_stop", REDIS, "stop", "Stop Redis/Valkey (job queue down)", 0.5),
    FaultSpec("db_pause", POSTGRES, "pause", "Freeze Postgres (database hung)", 0.5),
    FaultSpec("db_stop", POSTGRES, "stop", "Stop Postgres gracefully (database down)", 0.5),
    FaultSpec("server_restart", SERVER, "restart", "Restart the API server", 0.8),
    FaultSpec("ml_cpu_throttle", ML, "cpu", "Limit ML to 0.25 CPU (overloaded dependency)", 0.6),
    FaultSpec("ml_latency", ML, "latency", "Add 3000 ms network delay to ML (netem)", 0.6, optional=True),
]}
DEFAULT_EXPERIMENTS = ["ml_pause", "ml_stop", "redis_pause", "redis_stop", "db_pause", "server_restart"]


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
        return {"type": "set_cpus", "container": c, "nano_cpus": ctx.docker.state(c)["nano_cpus"] or 0}
    if spec.kind == "latency":
        return {"type": "netem", "container": c, "helper": NETEM_HELPER, "image": NETEM_IMAGE}
    raise FaultError(f"no recipe for kind {spec.kind}")


def apply_fault_action(ctx, spec: FaultSpec) -> threading.Thread | None:
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
        d.set_nano_cpus(c, 250_000_000)
    elif spec.kind == "latency":
        if not d.image_present(NETEM_IMAGE):
            raise FaultError(f"ml_latency needs the {NETEM_IMAGE} image: run `docker pull {NETEM_IMAGE}` first")
        d.run_helper(NETEM_HELPER, NETEM_IMAGE, c, ["sleep", "600"])
        d.exec(NETEM_HELPER, ["tc", "qdisc", "add", "dev", "eth0", "root", "netem", "delay", "3000ms"])
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
    elif kind == "set_cpus":
        _restore_cpus(ctx, c, int(recipe["nano_cpus"]))
    elif kind == "netem":
        _remove_netem(ctx, recipe)
    elif kind == "system_config":
        ctx.admin(fresh=True).put_system_config(recipe["config"])
    elif kind == "compose_override":
        restore_override(ctx, recipe.get("original"))
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
    if kind == "set_cpus":
        now = d.state(c)["nano_cpus"] or 0
        return now == int(recipe["nano_cpus"]), f"NanoCpus={now} expected={recipe['nano_cpus']}"
    if kind == "netem":
        if d.list_helpers():
            return False, f"helpers still present: {d.list_helpers()}"
        has = netem_present(ctx)
        return has is not True, "no netem qdisc" if has is False else ("netem check skipped" if has is None else "netem still active")
    if kind == "system_config":
        current = ctx.admin(fresh=True).get_system_config()
        same = json.dumps(current, sort_keys=True) == json.dumps(recipe["config"], sort_keys=True)
        return same, "system-config matches original" if same else "system-config differs from original"
    if kind == "compose_override":
        path = Path(ctx.settings.immich_dir) / OVERRIDE_FILE
        original = recipe.get("original")
        content = path.read_text(encoding="utf-8") if path.exists() else None
        ml2 = d.state(ML2)["exists"]
        expect_ml2 = original is not None and ML2 in (original or "")
        ok = content == original and ml2 == expect_ml2
        return ok, f"override {'restored' if content == original else 'differs'}, {ML2} exists={ml2}"
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


def compose_files(ctx, with_override: bool) -> list[str]:
    return ["docker-compose.yml"] + ([OVERRIDE_FILE] if with_override else [])


def restore_override(ctx, original: str | None) -> None:
    """Put the override file back as it was and bring compose in line with it."""
    path = Path(ctx.settings.immich_dir) / OVERRIDE_FILE
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(original, encoding="utf-8")
    ctx.docker.compose(Path(ctx.settings.immich_dir), compose_files(ctx, original is not None),
                       ["up", "-d", "--remove-orphans"])
    for name in CORE_CONTAINERS:
        ctx.docker.wait_healthy(name, ctx.settings.timings.healthy_timeout_s, ctx.settings.timings.poll_s)


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


def apply_fault(ctx, spec: FaultSpec, ttl_s: float | None = None) -> ActiveFault:
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
    ctx.log(f"APPLY {spec.id}: {spec.description} (TTL {ttl:.0f}s, journal entry {entry_id})")
    try:
        active.thread = apply_fault_action(ctx, spec)
    except Exception:
        ctx.log(f"apply of {spec.id} failed; undoing immediately")
        undo_fault(ctx, active)
        raise
    return active


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
