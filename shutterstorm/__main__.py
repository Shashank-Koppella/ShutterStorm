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
    p = add("baseline", "run all probes in a healthy state")
    p.add_argument("--rounds", type=int, default=None)
    p = add("inject", "inject one fault with TTL + kill switch")
    p.add_argument("fault")
    p.add_argument("--duration", type=float, default=20.0)
    p = add("run", "full workflow: baseline, experiments, analysis, optional repair, report")
    p.add_argument("--experiments", default="", help="comma-separated fault ids")
    p.add_argument("--repair", action="store_true", help="let the Repair agent try config-level fixes")
    p.add_argument("--keep-fix", action="store_true", help="leave a verified patch applied")
    add("panic", "kill switch: undo everything and restore the checkpoint")
    p = add("drill", "guard drill: inject ml_pause, kill the main process, watch the guard restore")
    p.add_argument("--fault", default="ml_pause")
    p = add("report", "regenerate the report of a saved run")
    p.add_argument("--run", required=True, help="run folder name (or path)")
    add("list", "show the fault catalog and the allowed repair knobs")
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
