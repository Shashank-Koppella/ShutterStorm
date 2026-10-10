"""Clean-state checkpoint and cleanup verification.

A checkpoint records everything ShutterStorm might change: container states and
CPU limits, the Immich system-config, the compose override file and a hash of
every file in IMMICH_DIR (except ``library/``). After a run, every item is
compared against it and the result goes into the report.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .config import CORE_CONTAINERS, ML2, OVERRIDE_FILE
from .context import write_json
from .faults import netem_present


def hash_dir(root: Path) -> dict[str, str]:
    """SHA-256 of every file under ``root`` except the ``library/`` media folder."""
    hashes: dict[str, str] = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root and "library" in dirnames:
            dirnames.remove("library")  # prune: never walk the (large) media folder
        for name in filenames:
            path = Path(dirpath) / name
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(hashes.items()))


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths that were added, removed or modified."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def take_checkpoint(ctx, run_dir: Path | None = None) -> dict:
    """Snapshot the current state and store it (run folder + ``last_checkpoint.json``)."""
    override = Path(ctx.settings.immich_dir) / OVERRIDE_FILE
    checkpoint = {
        "time": time.time(),
        "containers": {name: ctx.docker.state(name) for name in CORE_CONTAINERS},
        "ml2_exists": ctx.docker.state(ML2)["exists"],
        "system_config": ctx.admin().get_system_config(),
        "override": override.read_text(encoding="utf-8") if override.exists() else None,
        "files": hash_dir(Path(ctx.settings.immich_dir)),
    }
    write_json(ctx.journal.state_dir / "last_checkpoint.json", checkpoint)
    if run_dir:
        write_json(run_dir / "checkpoint" / "checkpoint.json", checkpoint)
    return checkpoint


def load_last_checkpoint(ctx) -> dict | None:
    path = ctx.journal.state_dir / "last_checkpoint.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def preflight_problems(ctx) -> list[str]:
    """Reasons the system is *not* in a clean state to start experiments."""
    problems = []
    for name in CORE_CONTAINERS:
        st = ctx.docker.state(name)
        if not st["exists"]:
            problems.append(f"{name} does not exist")
        elif st["paused"] or not st["running"] or st["health"] not in (None, "healthy"):
            problems.append(f"{name} not healthy (running={st['running']} paused={st['paused']} health={st['health']})")
        elif st.get("project") != "immich":
            problems.append(f"{name} is not part of compose project 'immich'")
    if ctx.journal.entries():
        problems.append("journal has pending undo entries (run `python -m shutterstorm panic`)")
    helpers = ctx.docker.list_helpers()
    if helpers:
        problems.append(f"leftover helper containers: {helpers}")
    return problems


def verify_cleanup(ctx, checkpoint: dict, tracker=None, keep_fix: bool = False) -> dict:
    """Compare the current state with ``checkpoint``. Returns {ok, checks: [...]}."""
    t = ctx.settings.timings
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for name in CORE_CONTAINERS:
        healthy = ctx.docker.wait_healthy(name, t.healthy_timeout_s, t.poll_s)
        st = ctx.docker.state(name)
        check(f"{name} running and healthy", healthy, f"health={st['health']}")
        check(f"{name} not paused", not st["paused"], f"paused={st['paused']}")
        expected = checkpoint["containers"][name]["nano_cpus"]
        check(f"{name} NanoCpus restored", st["nano_cpus"] == expected, f"{st['nano_cpus']} (expected {expected})")
    helpers = ctx.docker.list_helpers()
    check("no shutterstorm_* containers", not helpers, ", ".join(helpers) or "none")
    netem = netem_present(ctx)
    check("no netem qdisc on ML", netem is not True,
          {True: "netem still active", False: "none", None: "netshoot image absent, ml_latency never ran"}[netem])

    if keep_fix:
        check("system-config / override", True, "skipped: --keep-fix leaves the verified patch applied")
    else:
        try:
            current = ctx.admin(fresh=True).get_system_config()
            same = json.dumps(current, sort_keys=True) == json.dumps(checkpoint["system_config"], sort_keys=True)
            check("system-config equals checkpoint", same, "identical" if same else "differs")
        except Exception as exc:  # noqa: BLE001
            check("system-config equals checkpoint", False, f"could not read: {exc}")
        override = Path(ctx.settings.immich_dir) / OVERRIDE_FILE
        content = override.read_text(encoding="utf-8") if override.exists() else None
        check("override file state restored", content == checkpoint["override"],
              "absent" if content is None else "present")
        ml2 = ctx.docker.state(ML2)["exists"]
        check(f"{ML2} state restored", ml2 == checkpoint["ml2_exists"], f"exists={ml2}")

    changed = [p for p in changed_files(checkpoint["files"], hash_dir(Path(ctx.settings.immich_dir)))
               if not (keep_fix and p == OVERRIDE_FILE)]
    check("IMMICH_DIR files unchanged", not changed, ", ".join(changed) or "no changes")

    if tracker is not None:
        try:
            result = tracker.cleanup(ctx.admin(fresh=True))
            left = result["remaining"] + result["pending"]
            check("probe assets deleted", not left, f"deleted {result['deleted']}, left {len(left)}")
        except Exception as exc:  # noqa: BLE001
            check("probe assets deleted", False, f"cleanup failed: {exc}")
    pending = ctx.journal.entries()
    check("journal empty", not pending, f"{len(pending)} entries")
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "time": time.time()}
