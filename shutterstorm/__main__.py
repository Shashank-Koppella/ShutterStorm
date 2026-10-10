"""ShutterStorm command line: ``python -m shutterstorm <command>``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import commands
from .config import load_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shutterstorm", description="AI-driven chaos engineering for Immich.")
    parser.add_argument("--simulate", action="store_true", help="use fake Docker + fake Immich (offline, safe)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--simulate", action="store_true", default=argparse.SUPPRESS,
                       help="use fake Docker + fake Immich")
        return p

    add("doctor", "check Docker, containers, scope labels, Immich login, Mistral key, disk")
    add("discover", "build the dependency map (topology.json + Mermaid)")
    p = add("seed", "upload a folder of images as test content")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--limit", type=int, default=0, help="max images (0 = all)")
    p.add_argument("--wait", action="store_true", help="also wait until thumbnails/ML jobs finish and time it")
    p = add("baseline", "run all probes in a healthy state")
    p.add_argument("--rounds", type=int, default=None)
    def thresholds(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-error-rate", type=float, default=None,
                       help="roll back when more than this fraction of calls fail (default 0.75)")
        p.add_argument("--max-p95-ms", type=float, default=None, help="roll back above this p95 latency (default 8000)")
        p.add_argument("--breach-rounds", type=int, default=None,
                       help="consecutive breached probe rounds before rolling back (default 2)")
        p.add_argument("--no-blast-radius", action="store_true", help="do not stop when unrelated journeys fail")

    p = add("inject", "inject one fault with TTL + kill switch + safety thresholds")
    p.add_argument("fault")
    p.add_argument("--duration", type=float, default=20.0)
    thresholds(p)
    p = add("plan", "dry run: show which experiments the AI planner would choose, and why (injects nothing)")
    p.add_argument("--budget", type=int, default=6)
    p = add("run", "full workflow: AI plan, baseline, experiments, analysis, fix suggestions, report")
    p.add_argument("--experiments", default="", help="comma-separated fault ids (skips the AI planner)")
    p.add_argument("--budget", type=int, default=6, help="how many experiments the planner selects")
    p.add_argument("--no-plan", action="store_true", help="skip the planner and run the fixed default list")
    p.add_argument("--compare-to", default=None, help="earlier run to compare against (before/after a fix)")
    p.add_argument("--no-suggestions", action="store_true",
                   help="skip the Fix Advisor (suggestions are never applied either way)")
    thresholds(p)
    p = add("compare", "before/after comparison of two runs (e.g. after a human applied a suggested fix)")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    add("panic", "kill switch: undo everything and restore the checkpoint")
    p = add("drill", "guard drill: inject ml_pause, kill the main process, watch the guard restore")
    p.add_argument("--fault", default="ml_pause")
    p = add("report", "regenerate the report of a saved run")
    p.add_argument("--run", required=True, help="run folder name (or path)")
    p.add_argument("--reanalyze", action="store_true",
                   help="re-run only the AI explanations on the saved evidence (no Docker/Immich access)")
    add("list", "show the fault catalog and the settings fix suggestions may use")
    p = add("dashboard", "local web UI: inject a fault, watch it live, see the suggested fix")
    p.add_argument("--port", type=int, default=8765)
    add("stop", "create the STOP file (the running workflow stops at the next step)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(simulate=bool(getattr(args, "simulate", False)))
    handler = getattr(commands, f"cmd_{args.command}")
    try:
        return int(handler(settings, args) or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
