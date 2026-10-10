"""Human-in-the-loop approval for repair patches.

Before the repair loop applies a patch, the operator sees, in the CLI:

* the issue (which journeys broke and how, the score, the severity),
* the proposed fix (who proposed it, why, the exact changes and their side effects),
* how the fix will be tested,

and answers ``y`` (apply), ``n`` (decline, ask for a different fix), ``s`` (skip repairs
for this fault) or ``q`` (quit the run; cleanup still runs).

Modes:

* ``ask``  - prompt on the terminal (default). Without an interactive terminal nothing is
             applied: the decision is recorded as ``not_approved``.
* ``yes``  - ``--yes``: approve automatically (CI, unattended demos). Recorded as such.
"""
from __future__ import annotations

import sys
import time
from typing import Callable

from .config import OVERRIDE_FILE
from .probes import JOURNEYS

CHOICES = {"y": "approve", "yes": "approve", "n": "decline", "no": "decline",
           "s": "skip", "skip": "skip", "q": "quit", "quit": "quit"}
WIDTH = 64


def _wrap(label: str, text: str, width: int = WIDTH) -> list[str]:
    """Indent ``text`` under a 10-character label, wrapping long lines."""
    lines, current = [], ""
    for word in str(text).split():
        if current and len(current) + 1 + len(word) > width - 10:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return [f"{label:<10}{lines[0]}"] + [" " * 10 + line for line in lines[1:]]


def describe_issue(exp: dict) -> list[str]:
    """What broke, from measured data only."""
    parts = []
    for journey, cell in exp["heatmap"].items():
        if cell in ("ok", "n/a"):
            continue
        s = exp["fault_summary"].get(journey, {})
        detail = f" ({s['hung']}/{s['calls']} calls timed out)" if cell == "hung" and s else (
            f" ({s['fast_fail']}/{s['calls']} calls failed fast)" if cell == "fast-fail" and s else "")
        parts.append(f"{journey} {JOURNEYS[journey]}: {cell.upper()}{detail}")
    sc = exp["scoring"]
    lines = _wrap("Issue", f"{exp['fault']}: " + ("; ".join(parts) or "no journey failed"))
    lines += _wrap("", f"Score {sc['score']}/100, severity {sc['severity']} ({sc['severity_label']}). "
                       f"Other journeys OK.")
    cause = exp.get("analysis", {}).get("root_cause_hypothesis")
    if cause:
        src = exp["analysis"].get("source", "fallback")
        lines += _wrap("Cause", f"({'AI' if src in ('live', 'cache') else 'template'}, unverified) {cause[:300]}")
    return lines


def describe_effects(patch: dict) -> str:
    effects = []
    services = (patch.get("compose_override") or {}).get("services", {})
    if services:
        effects.append(f"writes {OVERRIDE_FILE} in IMMICH_DIR and runs docker compose up -d")
    if "immich-machine-learning-2" in services:
        effects.append("starts container immich_machine_learning_2 (~1-2 GB extra RAM)")
    other = [s for s in services if s != "immich-machine-learning-2"]
    if other:
        effects.append(f"may recreate {', '.join(other)} (brief downtime)")
    if patch.get("system_config"):
        effects.append("changes Immich system-config via the admin API")
    return "; ".join(effects) + ". Everything is journaled and reverted after the test (unless --keep-fix)."


def build_request(exp: dict, attempt: dict, n: int, max_attempts: int) -> str:
    """The full text shown to the operator."""
    source = attempt.get("source", "fallback")
    who = {"live": "AI (live)", "cache": "AI (cached answer)"}.get(source, "deterministic fallback")
    lines = ["", "=" * WIDTH, "REPAIR APPROVAL NEEDED".center(WIDTH), "=" * WIDTH]
    lines += describe_issue(exp)
    lines += ["", f"Proposed fix  attempt {n} of {max_attempts}, proposed by: {who}"]
    lines += _wrap("  Why", attempt.get("rationale") or "-")
    if attempt.get("expected_effect"):
        lines += _wrap("  Expects", attempt["expected_effect"])
    lines += ["  Changes"] + ["    " + line for line in attempt["diff"].splitlines()]
    lines += _wrap("  Effects", describe_effects(attempt["patch"]))
    lines += _wrap("  Test", f"protected journeys must pass, then {exp['fault']} is re-injected; pass = an "
                             f"affected journey improves and the score rises enough; the result is measured, "
                             f"not decided by the AI.")
    lines += ["", "Apply this fix?",
              "  [y] yes   [n] no, ask for another fix",
              "  [s] skip repairs for this fault   [q] quit the run (cleanup still runs)"]
    return "\n".join(lines)


class Approver:
    """Asks the operator (or auto-approves with ``--yes``) and records the decision."""

    def __init__(self, mode: str = "ask", ask: Callable[[str], str] | None = None,
                 out: Callable[[str], None] | None = None, interactive: bool | None = None) -> None:
        if mode not in ("ask", "yes"):
            raise ValueError("approval mode must be 'ask' or 'yes'")
        self.mode = mode
        self._ask = ask or input
        self._out = out or (lambda text: print(text, flush=True))
        self.interactive = sys.stdin.isatty() if interactive is None else interactive

    def request(self, text: str) -> dict:
        """Show ``text`` and return {decision, by, at}."""
        self._out(text)
        if self.mode == "yes":
            self._out("> y   (auto-approved: --yes)")
            return self._decision("approve", "--yes")
        if not self.interactive:
            self._out("> no interactive terminal: NOT applied (run in a terminal, or pass --yes)")
            return self._decision("not_approved", "no terminal")
        while True:
            try:
                answer = self._ask("> ").strip().lower()
            except EOFError:
                return self._decision("not_approved", "no input")
            if answer in CHOICES:
                return self._decision(CHOICES[answer], "operator")
            self._out("Please answer y, n, s or q.")

    @staticmethod
    def _decision(decision: str, by: str) -> dict:
        return {"decision": decision, "by": by, "at": time.strftime("%H:%M:%S")}
