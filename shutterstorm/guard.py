"""Independent dead-man guard: ``python -m shutterstorm.guard``.

Started (detached) whenever a fault is applied. It watches the journal and
undoes an entry when either

* the process that created it is gone (crash, kill, closed terminal), or
* the entry's TTL deadline has passed (plus a grace period).

It exits once the journal is empty. Because it is a separate OS process, it
restores the system even if the main ShutterStorm process is killed.

In the container setup the guard is its own long-running container (``--forever``) and the
controller does not spawn one (``SHUTTERSTORM_GUARD=external``). Process ids do not cross
containers, so for an entry written on another host the guard checks the owner's heartbeat
file instead (``journal.start_heartbeat``): no beat for ``HEARTBEAT_STALE_S`` = owner is gone.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

GRACE_S = 10.0
MAX_LIFETIME_S = 3600.0
HEARTBEAT_STALE_S = 15.0


def pid_alive(pid: int) -> bool:
    """Cross-platform "is this process still running?" (never sends a signal on Windows)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def entry_is_orphaned(entry: dict, now: float, alive=pid_alive, state_dir: Path | None = None) -> str | None:
    """Return why the guard should undo ``entry`` now, or ``None`` to keep waiting."""
    from .journal import heartbeat_age

    host = entry.get("host")
    if host and host != socket.gethostname() and state_dir is not None:
        age = heartbeat_age(state_dir, entry.get("owner", ""))
        if age is None or age > HEARTBEAT_STALE_S:
            return (f"owner {entry.get('owner')} on {host} stopped sending heartbeats"
                    + (f" {age:.0f}s ago" if age is not None else ""))
    elif not alive(int(entry.get("pid", 0))):
        return f"owner process {entry.get('pid')} is gone"
    deadline = entry.get("deadline")
    if deadline and now > deadline + GRACE_S:
        return f"TTL deadline passed {now - deadline:.0f}s ago"
    return None


def guard_loop(ctx, poll_s: float = 1.0, alive=pid_alive, max_lifetime_s: float = MAX_LIFETIME_S,
               forever: bool = False) -> int:
    """Watch the journal until it is empty (or forever). Returns the number of entries undone."""
    from .faults import undo_entry

    started = time.time()
    undone = 0
    while forever or time.time() - started < max_lifetime_s:
        if forever:
            _beat_guard(ctx)
        entries = ctx.journal.entries()
        if not entries and not forever:
            ctx.log("[guard] journal empty, exiting")
            return undone
        for entry in entries:
            reason = entry_is_orphaned(entry, time.time(), alive, ctx.journal.state_dir)
            if reason:
                ctx.log(f"[guard] taking over {entry['label']}: {reason}")
                ok, _ = undo_entry(ctx, entry, who="guard")
                undone += int(ok)
        time.sleep(poll_s)
    ctx.log("[guard] max lifetime reached, exiting")
    return undone


def _guard_pid_file(ctx) -> Path:
    return ctx.journal.state_dir / "guard.pid"


def _beat_guard(ctx) -> None:
    """The long-running (container) guard's own heartbeat, so the controller can see it is alive."""
    try:
        (ctx.journal.state_dir / "guard.heartbeat").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def external_guard_alive(ctx, max_age_s: float = 10.0) -> bool:
    try:
        return time.time() - float((ctx.journal.state_dir / "guard.heartbeat").read_text()) < max_age_s
    except (OSError, ValueError):
        return False


def spawn_guard(ctx) -> None:
    """Start the guard unless one is already running.

    Real mode: a detached OS process that survives the parent. Simulation mode:
    a daemon thread (the fake Docker lives inside this process).
    """
    if os.environ.get("SHUTTERSTORM_GUARD") == "external":
        if external_guard_alive(ctx):
            return  # the guard container watches the journal
        ctx.log("external guard container not seen in the last 10 s; starting a local guard as a fallback")
    if ctx.simulate:
        if getattr(ctx, "_sim_guard", None) and ctx._sim_guard.is_alive():
            return
        thread = threading.Thread(target=guard_loop, args=(ctx, 0.1), daemon=True)
        ctx._sim_guard = thread
        thread.start()
        return
    pid_file = _guard_pid_file(ctx)
    if pid_file.exists():
        try:
            if pid_alive(int(pid_file.read_text().strip())):
                return
        except ValueError:
            pass
    cmd = [sys.executable, "-m", "shutterstorm.guard", "--home", str(ctx.settings.home)]
    log = (ctx.journal.state_dir / "guard.log").open("a", encoding="utf-8")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "cwd": str(ctx.settings.home)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    ctx.log(f"guard started (pid {proc.pid}), log: {ctx.journal.state_dir / 'guard.log'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shutterstorm.guard")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--forever", action="store_true", help="keep watching (guard container)")
    args = parser.parse_args(argv)
    from .config import load_settings
    from .context import build_context

    ctx = build_context(load_settings(home=args.home))
    _guard_pid_file(ctx).write_text(str(os.getpid()), encoding="utf-8")
    ctx.log(f"[guard] started, pid {os.getpid()}")
    try:
        guard_loop(ctx, poll_s=1.0, forever=args.forever)
    finally:
        _guard_pid_file(ctx).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
