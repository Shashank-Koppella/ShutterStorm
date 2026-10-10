"""Run context: settings, Docker, journal, console and the Immich admin client.

``build_context`` is the single place where simulation mode is wired in: with
``--simulate`` the Docker backend and the Immich server are both fakes from
:mod:`shutterstorm.sim`, and every line of output carries a SIMULATED marker.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import SIM_TIMINGS, Settings, redact
from .dockerctl import DockerCtl
from .events import NULL as NULL_EVENTS
from .events import EventLog
from .immich import ImmichClient
from .journal import Journal

SIM_BANNER = "*** SIMULATED: fake Docker + fake Immich. Not a real finding. ***"


class Console:
    """Prints timestamped, redacted lines and mirrors them to a log file."""

    def __init__(self, secrets: list[str], simulate: bool = False) -> None:
        self.secrets = secrets
        self.simulate = simulate
        self.log_path: Path | None = None
        self._lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        self.info(msg)

    def info(self, msg: str) -> None:
        prefix = "[SIM] " if self.simulate else ""
        line = f"{time.strftime('%H:%M:%S')} {prefix}{redact(str(msg), self.secrets)}"
        with self._lock:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode(), flush=True)
            if self.log_path:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")


@dataclass
class Ctx:
    """Everything a command needs."""

    settings: Settings
    docker: DockerCtl
    journal: Journal
    console: Console
    sim: Any = None  # SimWorld when simulating
    events: EventLog = field(default=NULL_EVENTS, repr=False)
    _admin: ImmichClient | None = field(default=None, repr=False)

    @property
    def simulate(self) -> bool:
        return self.settings.simulate

    def log(self, msg: str) -> None:
        self.console.info(msg)

    def event(self, type_: str, **data) -> None:
        """Structured event for the dashboard (``runs/<id>/events.jsonl``); never raises."""
        self.events.emit(type_, **data)

    def admin(self, fresh: bool = False) -> ImmichClient:
        """A logged-in admin client (cached)."""
        if self._admin is None or fresh:
            client = ImmichClient(self.settings.immich_url, self.settings.probe_timeout_seconds)
            resp = client.login(self.settings.immich_email, self.settings.immich_password)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Immich admin login failed: HTTP {resp.status_code}")
            self._admin = client
        return self._admin

    def new_run_dir(self, kind: str) -> Path:
        """Create ``runs/<timestamp>-<kind>[-SIM]/`` and start logging into it."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{kind}" + ("-SIM" if self.simulate else "")
        run_dir = self.settings.runs_dir / name
        n = 1
        while run_dir.exists():
            n += 1
            run_dir = self.settings.runs_dir / f"{name}-{n}"
        run_dir.mkdir(parents=True)
        if self.simulate:
            (run_dir / "SIMULATED.txt").write_text(SIM_BANNER + "\n", encoding="utf-8")
        self.console.log_path = run_dir / "run.log"
        self.events = EventLog(run_dir / "events.jsonl")
        self.console.info(f"run folder: {run_dir.name}")
        return run_dir

    def close(self) -> None:
        if self.sim is not None:
            self.sim.shutdown()


def write_json(path: Path, data: Any) -> None:
    """Write pretty JSON (creating parent folders)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def build_context(settings: Settings) -> Ctx:
    """Create a context; in simulation mode, start the fake world first."""
    sim = None
    if settings.simulate:
        from .sim import SimWorld  # imported lazily: never needed for real runs

        sim = SimWorld(settings.state_dir / "sim_immich")
        settings = settings.with_overrides(
            immich_url=sim.url, immich_dir=sim.immich_dir, immich_email=sim.EMAIL,
            immich_password=sim.PASSWORD, timings=SIM_TIMINGS,
            probe_timeout_seconds=min(settings.probe_timeout_seconds, 1.0),
            max_fault_seconds=min(settings.max_fault_seconds, 8.0),
            # Never spend real API calls on fake data unless explicitly asked to.
            llm_mode=settings.llm_mode if os.environ.get("SHUTTERSTORM_SIM_LLM") else "off",
        )
        print(SIM_BANNER, flush=True)
    console = Console(settings.secrets(), settings.simulate)
    state_dir = settings.state_dir / ("sim" if settings.simulate else "")
    journal = Journal(state_dir)
    if settings.simulate:
        # A fresh fake world has nothing to undo; leftovers belong to a dead simulation.
        for name in ("journal.json", "probe_assets.json", "last_checkpoint.json", "STOP"):
            (state_dir / name).unlink(missing_ok=True)
    docker = DockerCtl(sim.docker if sim else None, log=console.info)
    return Ctx(settings=settings, docker=docker, journal=journal, console=console, sim=sim)
