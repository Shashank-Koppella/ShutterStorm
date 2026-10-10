"""Implementations of the CLI commands (kept thin; the logic lives in other modules)."""
from __future__ import annotations

import shutil
from argparse import Namespace

from .config import CORE_CONTAINERS, COMPOSE_PROJECT, Settings
from .context import Ctx, build_context
from .probes import AssetTracker, Prober
from .reporting_io import append_probes_csv, summary_table


def _ctx(settings: Settings) -> Ctx:
    return build_context(settings)


def make_prober(ctx: Ctx, run_tag: str) -> Prober:
    tracker = AssetTracker(ctx.journal.state_dir / "probe_assets.json")
    prober = Prober(ctx.settings, tracker, run_tag)
    prober.authenticate()
    return prober


# ------------------------------------------------------------------------ doctor
def cmd_doctor(settings: Settings, args: Namespace) -> int:
    """Check everything a run needs; prints one line per check."""
    ctx = _ctx(settings)
    checks: list[tuple[str, bool, str]] = []
    try:
        version = ctx.docker.version()
        checks.append(("docker reachable", version is not None, f"server {version}"))
        for name in CORE_CONTAINERS:
            st = ctx.docker.state(name)
            ok = st["running"] and not st["paused"] and st["health"] in (None, "healthy") \
                and st.get("project") == COMPOSE_PROJECT
            checks.append((f"container {name}", ok,
                           f"running={st['running']} paused={st['paused']} health={st['health']} "
                           f"project={st.get('project')} nano_cpus={st['nano_cpus']}"))
        compose_file = ctx.settings.immich_dir / "docker-compose.yml"
        checks.append(("IMMICH_DIR has docker-compose.yml", compose_file.exists(), str(ctx.settings.immich_dir)))
        try:
            client = ctx.admin()
            ver = client.version().json()
            checks.append(("Immich login", True, f"v{ver['major']}.{ver['minor']}.{ver['patch']}"))
        except Exception as exc:  # noqa: BLE001 - doctor reports every failure
            checks.append(("Immich login", False, str(exc)))
        mode = ctx.settings.llm_mode
        has_key = bool(ctx.settings.mistral_api_key)
        checks.append(("Mistral key", has_key or mode != "live",
                       f"LLM_MODE={mode}, key {'set' if has_key else 'missing'}"))
        free_gb = shutil.disk_usage(ctx.settings.home).free / 1e9
        checks.append(("disk space", free_gb > 2, f"{free_gb:.1f} GB free"))
        pending = ctx.journal.entries()
        checks.append(("journal empty", not pending, f"{len(pending)} pending entries"))
        checks.append(("no STOP file", not ctx.journal.stop_requested(), str(ctx.journal.stop_path)))
        helpers = ctx.docker.list_helpers()
        checks.append(("no leftover helpers", not helpers, ", ".join(helpers) or "none"))
    finally:
        ctx.close()
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name:<36} {detail}")
    failed = [c for c in checks if not c[1]]
    print("All checks passed." if not failed else f"{len(failed)} check(s) failed.")
    return 1 if failed else 0


# ------------------------------------------------------------------------ baseline
def cmd_baseline(settings: Settings, args: Namespace) -> int:
    """Run all journeys a few times in a healthy state and print p50/p95."""
    ctx = _ctx(settings)
    try:
        run_dir = ctx.new_run_dir("baseline")
        prober = make_prober(ctx, run_dir.name)
        prober.warmup()
        rounds = args.rounds or ctx.settings.timings.baseline_rounds
        results = []
        for i in range(rounds):
            r = prober.run_round("baseline", async_deadline=ctx.settings.timings.async_deadline_s)
            ctx.log(f"round {i + 1}/{rounds}: " + " ".join(f"{x.journey}={x.outcome}" for x in r))
            results += r
        append_probes_csv(run_dir / "probes.csv", results)
        print(summary_table(results))
        cleanup = prober.tracker.cleanup(ctx.admin())
        ctx.log(f"probe assets deleted: {cleanup['deleted']}, remaining: {len(cleanup['remaining'])}")
        ctx.log(f"saved {run_dir}")
        return 0 if all(r.ok for r in results) else 1
    finally:
        ctx.close()
