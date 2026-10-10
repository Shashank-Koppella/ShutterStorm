"""Settings for ShutterStorm.

Values come from ``shutterstorm.env`` (a simple KEY=VALUE file, gitignored) and
can be overridden by real environment variables. Secrets are never printed:
use :func:`redact` before logging anything that might contain them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

ENV_FILE_NAME = "shutterstorm.env"

# The only containers ShutterStorm may ever act on (plus shutterstorm_* helpers).
COMPOSE_PROJECT = "immich"
SERVER = "immich_server"
ML = "immich_machine_learning"
REDIS = "immich_redis"
POSTGRES = "immich_postgres"
ML2 = "immich_machine_learning_2"  # name used in suggested fixes (never created by ShutterStorm)
CORE_CONTAINERS = (SERVER, ML, REDIS, POSTGRES)
HELPER_PREFIX = "shutterstorm_"
PROBE_PREFIX = "shutterstorm-probe-"
OVERRIDE_FILE = "docker-compose.shutterstorm.yml"


@dataclass(frozen=True)
class Timings:
    """Durations used by the workflow. Simulation mode uses a much faster set."""

    baseline_rounds: int = 5
    fault_window_s: float = 35.0       # how long a fault stays applied while probing
    settle_s: float = 3.0              # wait after applying a fault before probing
    async_deadline_s: float = 90.0     # J5: max wait for a thumbnail
    fault_async_deadline_s: float = 8.0  # J5 during a fault window (short; checked again later)
    recovery_limit_s: float = 300.0    # stop waiting for recovery after this
    abort_liveness_s: float = 30.0     # abort the run if J0 fails this long after an undo
    poll_s: float = 1.0
    healthy_timeout_s: float = 180.0   # wait for a container to become healthy
    round_interval_s: float = 2.0      # minimum time between probe rounds (limits load)


@dataclass(frozen=True)
class Thresholds:
    """Impact limits checked after every probe round while a fault is active.

    If one is breached for ``rounds`` consecutive rounds, the fault is rolled back at
    once (before its window or TTL ends) and the experiment is marked auto-stopped.
    """

    max_error_rate: float = 0.75   # fraction of failed (error or hung) user calls in a round
    max_p95_ms: float = 8000.0     # p95 latency of the successful calls in a round
    blast_radius: bool = True      # a journey that does NOT depend on the faulted service fails
    rounds: int = 1                # consecutive breached rounds before rolling back

    def describe(self) -> str:
        return (f"error rate > {self.max_error_rate:.0%}, p95 > {self.max_p95_ms:.0f} ms"
                + (", or a journey outside the blast radius fails" if self.blast_radius else "")
                + f", for {self.rounds} consecutive round(s)")


SIM_TIMINGS = Timings(
    round_interval_s=0.1,
    baseline_rounds=2,
    fault_window_s=2.5,
    settle_s=0.2,
    async_deadline_s=3.0,
    fault_async_deadline_s=0.5,
    recovery_limit_s=10.0,
    abort_liveness_s=5.0,
    poll_s=0.1,
    healthy_timeout_s=5.0,
)


@dataclass(frozen=True)
class Settings:
    """All runtime configuration in one immutable object."""

    immich_url: str = "http://localhost:2283"
    immich_email: str = ""
    immich_password: str = ""
    immich_dir: Path = Path(".")
    mistral_api_key: str = ""
    mistral_model: str = "ministral-8b-latest"
    llm_mode: str = "live"  # live | replay | off
    max_fault_seconds: float = 60.0
    probe_timeout_seconds: float = 10.0
    llm_budget: int = 40
    home: Path = field(default_factory=Path.cwd)  # repo root: .shutterstorm/ and runs/ live here
    simulate: bool = False
    timings: Timings = field(default_factory=Timings)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def state_dir(self) -> Path:
        """Folder for the journal, STOP file, LLM cache and guard log."""
        return self.home / ".shutterstorm"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def api_url(self) -> str:
        return self.immich_url.rstrip("/") + "/api"

    def secrets(self) -> list[str]:
        """Values that must never appear in output."""
        return [s for s in (self.immich_password, self.mistral_api_key) if s]

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks, comments and trailing ``# comments``."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.sub(r"\s+#.*$", "", value).strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def load_settings(home: Path | None = None, simulate: bool = False) -> Settings:
    """Load settings from ``<home>/shutterstorm.env`` and the environment."""
    home = (home or Path(os.environ.get("SHUTTERSTORM_HOME", Path.cwd()))).resolve()
    values = parse_env_file(home / ENV_FILE_NAME)
    for key in list(values) + [
        "IMMICH_URL", "IMMICH_EMAIL", "IMMICH_PASSWORD", "IMMICH_DIR", "MISTRAL_API_KEY",
        "MISTRAL_MODEL", "LLM_MODE", "MAX_FAULT_SECONDS", "PROBE_TIMEOUT_SECONDS",
        "LLM_BUDGET", "ABORT_ERROR_RATE", "ABORT_P95_MS", "ABORT_ROUNDS", "ABORT_BLAST_RADIUS",
    ]:
        if os.environ.get(key):
            values[key] = os.environ[key]

    llm_mode = values.get("LLM_MODE", "live").lower()
    if llm_mode not in ("live", "replay", "off"):
        raise ValueError(f"LLM_MODE must be live, replay or off (got {llm_mode!r})")

    settings = Settings(
        immich_url=values.get("IMMICH_URL", "http://localhost:2283"),
        immich_email=values.get("IMMICH_EMAIL", ""),
        immich_password=values.get("IMMICH_PASSWORD", ""),
        immich_dir=Path(values.get("IMMICH_DIR", ".")).expanduser(),
        mistral_api_key=values.get("MISTRAL_API_KEY", ""),
        mistral_model=values.get("MISTRAL_MODEL", "ministral-8b-latest"),
        llm_mode=llm_mode,
        max_fault_seconds=float(values.get("MAX_FAULT_SECONDS", 60)),
        probe_timeout_seconds=float(values.get("PROBE_TIMEOUT_SECONDS", 10)),
        llm_budget=int(values.get("LLM_BUDGET", 40)),
        home=home,
        simulate=simulate,
        thresholds=Thresholds(
            max_error_rate=float(values.get("ABORT_ERROR_RATE", 0.75)),
            max_p95_ms=float(values.get("ABORT_P95_MS", 8000)),
            rounds=int(values.get("ABORT_ROUNDS", 1)),
            blast_radius=values.get("ABORT_BLAST_RADIUS", "on").lower() not in ("0", "off", "false", "no"),
        ),
    )
    return settings


_TOKEN_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
    re.compile(r'("?(?:accessToken|password|api_key|apiKey|DB_PASSWORD)"?\s*[:=]\s*"?)[^",\s}]+', re.I),
]


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove secrets and bearer tokens from ``text``."""
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(r"\1***", text)
    return text
