"""Docker CLI wrapper with a hard scope check.

All Docker access goes through :class:`DockerCtl`, which shells out to the
``docker`` CLI with argument lists (never ``shell=True``). A backend object does
the actual execution, so tests can swap in :class:`shutterstorm.sim.FakeDocker`.

Every *mutating* action calls :meth:`DockerCtl.ensure_in_scope` first: the
container must be one of the Immich containers (or a ``shutterstorm_*`` helper)
and must carry ``com.docker.compose.project=immich``.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import COMPOSE_PROJECT, CORE_CONTAINERS, HELPER_PREFIX, ML2

SCOPED_NAMES = set(CORE_CONTAINERS) | {ML2}
HELPER_LABEL = "shutterstorm.helper=true"


class ScopeError(RuntimeError):
    """Raised when an action targets something outside our sandbox."""


class DockerError(RuntimeError):
    """Raised when a docker command fails."""


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


class Backend(Protocol):
    def run(self, args: list[str], timeout: float = 60, cwd: Path | None = None) -> CmdResult: ...


class RealDocker:
    """Runs the real ``docker`` CLI."""

    def run(self, args: list[str], timeout: float = 60, cwd: Path | None = None) -> CmdResult:
        try:
            proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                                  cwd=str(cwd) if cwd else None, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return CmdResult(127, "", "docker CLI not found on PATH")
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", f"docker {' '.join(args[:3])} timed out after {timeout}s")
        return CmdResult(proc.returncode, proc.stdout, proc.stderr)


class DockerCtl:
    """High-level, scope-checked Docker operations."""

    def __init__(self, backend: Backend | None = None, log=None) -> None:
        self.backend = backend or RealDocker()
        self.log = log or (lambda msg: None)

    # ---------------------------------------------------------------- low level
    def _run(self, args: list[str], timeout: float = 60, cwd: Path | None = None, check: bool = True) -> CmdResult:
        result = self.backend.run(args, timeout=timeout, cwd=cwd)
        if check and result.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} failed: {result.stderr.strip()[:300]}")
        return result

    def inspect(self, name: str) -> dict | None:
        """``docker inspect`` one container; ``None`` if it does not exist."""
        result = self._run(["inspect", name], check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data[0] if data else None

    def state(self, name: str) -> dict:
        """Compact state: exists, running, paused, health, nano_cpus, project label."""
        info = self.inspect(name)
        if info is None:
            return {"exists": False, "running": False, "paused": False, "health": None, "nano_cpus": None}
        st = info.get("State", {})
        return {
            "exists": True,
            "running": bool(st.get("Running")),
            "paused": bool(st.get("Paused")),
            "status": st.get("Status"),
            "health": (st.get("Health") or {}).get("Status"),
            "nano_cpus": info.get("HostConfig", {}).get("NanoCpus", 0) or 0,
            "project": (info.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project"),
        }

    # ---------------------------------------------------------------- scope
    def ensure_in_scope(self, name: str) -> None:
        """Refuse any container that is not ours. Raises :class:`ScopeError`."""
        if name.startswith(HELPER_PREFIX):
            info = self.inspect(name)
            labels = (info or {}).get("Config", {}).get("Labels") or {}
            if info is not None and labels.get("shutterstorm.helper") != "true":
                raise ScopeError(f"{name} looks like a helper but lacks the {HELPER_LABEL} label")
            return
        if name not in SCOPED_NAMES:
            raise ScopeError(f"refusing to touch {name!r}: not one of {sorted(SCOPED_NAMES)}")
        info = self.inspect(name)
        if info is None:
            raise ScopeError(f"container {name} does not exist")
        project = (info.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project")
        if project != COMPOSE_PROJECT:
            raise ScopeError(f"refusing to touch {name}: compose project is {project!r}, expected {COMPOSE_PROJECT!r}")

    # ---------------------------------------------------------------- actions
    def _act(self, verb: str, name: str, *extra: str, timeout: float = 90) -> None:
        self.ensure_in_scope(name)
        self.log(f"docker {verb} {' '.join(extra)} {name}".replace("  ", " "))
        self._run([verb, *extra, name], timeout=timeout)

    def pause(self, name: str) -> None:
        self._act("pause", name)

    def unpause(self, name: str) -> None:
        self._act("unpause", name)

    def stop(self, name: str) -> None:
        self._act("stop", name, timeout=120)

    def start(self, name: str) -> None:
        self._act("start", name)

    def restart(self, name: str) -> None:
        self._act("restart", name, timeout=120)

    def set_nano_cpus(self, name: str, nano_cpus: int) -> None:
        """``docker update --cpus``; ``0`` means unlimited."""
        cpus = f"{nano_cpus / 1e9:g}" if nano_cpus else "0"
        self._act("update", name, "--cpus", cpus)

    def logs(self, name: str, tail: int = 80) -> str:
        """Last ``tail`` log lines (stdout + stderr). Read-only, so no scope check needed."""
        result = self._run(["logs", "--tail", str(tail), name], check=False, timeout=30)
        return (result.stdout + result.stderr)[-12000:]

    def list_helpers(self) -> list[str]:
        """Names of all ``shutterstorm_*`` containers (running or not)."""
        result = self._run(["ps", "-a", "--filter", f"name={HELPER_PREFIX}", "--format", "{{.Names}}"], check=False)
        return [n for n in result.stdout.split() if n.startswith(HELPER_PREFIX)]

    def run_helper(self, name: str, image: str, network_of: str, cmd: list[str], cap_add: str = "NET_ADMIN") -> None:
        """Start a labelled helper container sharing ``network_of``'s network namespace."""
        if not name.startswith(HELPER_PREFIX):
            raise ScopeError(f"helper names must start with {HELPER_PREFIX}")
        self.ensure_in_scope(network_of)
        self.log(f"docker run helper {name} in netns of {network_of}")
        self._run(["run", "-d", "--name", name, "--label", HELPER_LABEL, "--net", f"container:{network_of}",
                   "--cap-add", cap_add, image, *cmd], timeout=300)

    def exec(self, name: str, cmd: list[str], check: bool = True) -> CmdResult:
        self.ensure_in_scope(name)
        return self._run(["exec", name, *cmd], check=check, timeout=30)

    def remove_helper(self, name: str) -> None:
        self.ensure_in_scope(name)
        if not name.startswith(HELPER_PREFIX):
            raise ScopeError("only shutterstorm_* helpers may be removed")
        self.log(f"docker rm -f {name}")
        self._run(["rm", "-f", name], check=False)

    def image_present(self, image: str) -> bool:
        return self._run(["image", "inspect", image], check=False).returncode == 0

    # ---------------------------------------------------------------- compose
    def compose(self, immich_dir: Path, files: list[str], args: list[str], timeout: float = 600) -> CmdResult:
        """Run ``docker compose -f ... <args>`` in IMMICH_DIR (project name fixed to immich)."""
        file_args: list[str] = []
        for f in files:
            file_args += ["-f", f]
        self.log(f"docker compose {' '.join(file_args)} {' '.join(args)}")
        return self._run(["compose", "-p", COMPOSE_PROJECT, *file_args, *args], cwd=immich_dir, timeout=timeout)

    def compose_config(self, immich_dir: Path) -> dict:
        result = self._run(["compose", "-p", COMPOSE_PROJECT, "config", "--format", "json"], cwd=immich_dir)
        return json.loads(result.stdout)

    # ---------------------------------------------------------------- waiting
    def wait_healthy(self, name: str, timeout: float = 180, poll: float = 1.0) -> bool:
        """Wait until running, not paused and healthy (or no healthcheck).

        The wait is at least one healthcheck interval + 30 s: Immich's Postgres image checks only
        every 300 s, so after a pause it stays "unhealthy" for up to 5 minutes (API_NOTES #11).
        """
        info = self.inspect(name) or {}
        interval_s = ((info.get("Config", {}).get("Healthcheck") or {}).get("Interval") or 0) / 1e9
        if interval_s:
            timeout = max(timeout, interval_s + 30)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.state(name)
            if st["running"] and not st["paused"] and st["health"] in (None, "healthy"):
                return True
            time.sleep(poll)
        return False

    def version(self) -> str | None:
        result = self._run(["version", "--format", "{{.Server.Version}}"], check=False, timeout=20)
        return result.stdout.strip() if result.returncode == 0 else None
