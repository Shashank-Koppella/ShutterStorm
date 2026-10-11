"""Write-ahead undo journal and STOP file.

Before ShutterStorm applies a fault or a patch it writes an *undo recipe* (with
the original values) to ``.shutterstorm/journal.json``. The entry is removed
only after the undo has been verified. If the main process dies, the guard
process or ``panic`` reads the journal and undoes whatever is left.

The journal is shared between processes (main run, guard), so every update
takes a small lock file and writes atomically (temp file + ``os.replace``).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


HEARTBEAT_EVERY_S = 2.0
_heartbeats: dict[str, threading.Thread] = {}


def replace_atomic(tmp: Path, target: Path, attempts: int = 20) -> None:
    """``os.replace`` that survives Windows' transient "Access is denied" while another reader
    (or an antivirus scan of the fresh file) briefly holds the target open."""
    for i in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.02 * (i + 1))


def owner_id() -> str:
    """This process, as other containers can name it: ``<hostname>-<pid>``."""
    return f"{socket.gethostname()}-{os.getpid()}"


def heartbeat_path(state_dir: Path, owner: str) -> Path:
    return state_dir / "heartbeats" / f"{owner}.json"


def start_heartbeat(state_dir: Path) -> None:
    """Write ``heartbeats/<owner>.json`` every 2 s for as long as this process lives.

    A process id means nothing in another container, so a guard running in its own container
    decides whether the owner of a journal entry is alive from this file's age instead.
    """
    owner = owner_id()
    if owner in _heartbeats and _heartbeats[owner].is_alive():
        return
    path = heartbeat_path(state_dir, owner)
    path.parent.mkdir(parents=True, exist_ok=True)

    def beat() -> None:
        while True:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"t": time.time(), "owner": owner}), encoding="utf-8")
                replace_atomic(tmp, path)
            except OSError:
                pass
            time.sleep(HEARTBEAT_EVERY_S)

    thread = threading.Thread(target=beat, daemon=True, name="shutterstorm-heartbeat")
    _heartbeats[owner] = thread
    thread.start()


def heartbeat_age(state_dir: Path, owner: str) -> float | None:
    """Seconds since ``owner`` last beat, or ``None`` if it never did."""
    try:
        data = json.loads(heartbeat_path(state_dir, owner).read_text(encoding="utf-8"))
        return time.time() - float(data["t"])
    except (OSError, ValueError, KeyError):
        return None


class Journal:
    """A list of pending undo entries persisted as JSON."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "journal.json"
        self.lock_path = state_dir / "journal.lock"
        self.stop_path = state_dir / "STOP"
        state_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- locking
    @contextmanager
    def _locked(self, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                # A lock older than 15 s belongs to a crashed process: break it.
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 15:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError("journal lock is busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("entries", [])
        except json.JSONDecodeError:
            # Never silently drop undo information: keep a copy for a human to inspect.
            self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}.json"))
            return []

    def _write(self, entries: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")
        replace_atomic(tmp, self.path)

    # ---------------------------------------------------------------- API
    def add(self, kind: str, label: str, recipe: dict, ttl_s: float | None = None) -> str:
        """Record an undo recipe *before* the change is made. Returns the entry id."""
        entry = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,            # "fault" or "patch"
            "label": label,
            "recipe": recipe,
            "created_at": time.time(),
            "deadline": time.time() + ttl_s if ttl_s else None,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "owner": owner_id(),
        }
        start_heartbeat(self.state_dir)
        with self._locked():
            entries = self._read()
            entries.append(entry)
            self._write(entries)
        return entry["id"]

    def remove(self, entry_id: str) -> None:
        """Remove an entry once its undo has been verified."""
        with self._locked():
            self._write([e for e in self._read() if e["id"] != entry_id])

    def reassign_pid(self, entry_id: str, pid: int) -> None:
        """Change the owner pid of an entry (used by the simulated guard drill)."""
        with self._locked():
            entries = self._read()
            for e in entries:
                if e["id"] == entry_id:
                    e["pid"] = pid
            self._write(entries)

    def entries(self) -> list[dict]:
        with self._locked():
            return self._read()

    def get(self, entry_id: str) -> dict | None:
        return next((e for e in self.entries() if e["id"] == entry_id), None)

    def is_empty(self) -> bool:
        return not self.entries()

    # ---------------------------------------------------------------- STOP file
    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def request_stop(self) -> None:
        self.stop_path.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")

    def clear_stop(self) -> None:
        self.stop_path.unlink(missing_ok=True)
