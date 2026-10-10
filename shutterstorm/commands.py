"""Implementations of the CLI commands (kept thin; the logic lives in other modules)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from argparse import Namespace

from .checkpoint import preflight_problems, take_checkpoint, verify_cleanup
from .config import CORE_CONTAINERS, COMPOSE_PROJECT, Settings
from .context import Ctx, build_context, write_json
from .experiment import run_experiment
from .faults import apply_fault, get_fault
from .probes import AssetTracker, Prober, ProbeResult
from .reporting_io import append_probes_csv, summary_table
from .safety import AbortRun, panic


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


# ------------------------------------------------------------------------ inject
def print_round(ctx: Ctx):
    """Callback that logs one line per probe round."""
    def _cb(phase: str, n: int, results) -> None:
        ctx.log(f"{phase:<8} round {n}: " + "  ".join(
            f"{r.journey}={r.outcome}({r.latency_ms / 1000:.1f}s)" for r in results))
    return _cb


def cmd_inject(settings: Settings, args: Namespace) -> int:
    """Day-1 deliverable: one fault with journal, guard, TTL and Ctrl+C kill switch."""
    spec = get_fault(args.fault)
    ctx = _ctx(settings)
    run_dir = None
    checkpoint = None
    prober = None
    status = 0
    try:
        ctx.journal.clear_stop()
        problems = preflight_problems(ctx)
        if problems:
            for p in problems:
                ctx.log(f"not clean: {p}")
            return 2
        run_dir = ctx.new_run_dir(f"inject-{spec.id}")
        checkpoint = take_checkpoint(ctx, run_dir)
        prober = make_prober(ctx, run_dir.name)
        prober.warmup()
        base = prober.run_round("baseline", async_deadline=ctx.settings.timings.async_deadline_s)
        print_round(ctx)("baseline", 1, base)
        ctx.log(f"Injecting {spec.id} for {args.duration:.0f}s. Kill switches: Ctrl+C, "
                f"`python -m shutterstorm panic`, or create {ctx.journal.stop_path}")
        ev = run_experiment(ctx, prober, spec, run_dir, window_s=args.duration, on_round=print_round(ctx))
        print("\nDuring the fault:")
        print(summary_table([ProbeResult(**r) for r in ev["fault_results"]]))
        rec = "not within limit" if ev["recovery_s"] is None else f"{ev['recovery_s']}s after undo"
        ctx.log(f"recovery: {rec}; uploads stored {ev['integrity']['stored']}/{ev['integrity']['attempted']}, "
                f"intact {ev['integrity']['intact']}, ghost uploads {ev['integrity']['ghost_uploads']}")
    except AbortRun as exc:
        ctx.log(f"ABORT: {exc}")
        panic(ctx, reason=str(exc))
        status = 3
    except KeyboardInterrupt:
        ctx.log("Ctrl+C received: fault already undone by the experiment's cleanup path")
        status = 130
    finally:
        if checkpoint is not None:
            result = verify_cleanup(ctx, checkpoint, prober.tracker if prober else None)
            write_json(run_dir / "cleanup.json", result)
            for c in result["checks"]:
                if not c["ok"]:
                    ctx.log(f"cleanup check FAILED: {c['name']} ({c['detail']})")
            ctx.log(f"cleanup verification: {'PASS' if result['ok'] else 'FAIL'} ({len(result['checks'])} checks)")
            status = status or (0 if result["ok"] else 4)
        ctx.close()
    return status


# ------------------------------------------------------------------------ kill switches
def cmd_panic(settings: Settings, args: Namespace) -> int:
    ctx = _ctx(settings)
    try:
        result = panic(ctx, reason="manual panic command")
        return 0 if result["journal_left"] == 0 else 1
    finally:
        ctx.close()


def cmd_stop(settings: Settings, args: Namespace) -> int:
    ctx = _ctx(settings)
    ctx.journal.request_stop()
    print(f"STOP file created: {ctx.journal.stop_path}. A running workflow stops at its next step.")
    ctx.close()
    return 0


# ------------------------------------------------------------------------ drill
def cmd_drill(settings: Settings, args: Namespace) -> int:
    """Inject a fault, kill the process that owns it, and watch the guard restore it."""
    spec = get_fault(args.fault)
    ctx = _ctx(settings)
    try:
        ctx.journal.clear_stop()
        problems = preflight_problems(ctx)
        if problems:
            for p in problems:
                ctx.log(f"not clean: {p}")
            return 2
        take_checkpoint(ctx)
        ctx.log(f"GUARD DRILL: inject {spec.id}, then kill the owning process without any cleanup")
        if ctx.simulate:
            active = apply_fault(ctx, spec)
            active._timer.cancel()  # a dead process has no watchdog thread either
            time.sleep(1)
            ctx.log("simulated crash: owner process is gone (pid 0), TTL watchdog gone")
            ctx.journal.reassign_pid(active.entry_id, 0)
        else:
            child = subprocess.Popen([sys.executable, "-m", "shutterstorm", "inject", spec.id, "--duration", "50"],
                                     cwd=str(ctx.settings.home))
            deadline = time.time() + 120
            while time.time() < deadline and not ctx.journal.entries():
                time.sleep(0.5)
            if not ctx.journal.entries():
                child.kill()
                ctx.log("child never applied the fault; drill aborted")
                return 1
            time.sleep(6)
            ctx.log(f">>> KILLING main process pid {child.pid} (no cleanup code will run) <<<")
            child.kill()
            child.wait()
        start = time.time()
        while time.time() - start < 120:
            st = ctx.docker.state(spec.container)
            left = ctx.journal.entries()
            ctx.log(f"t+{time.time() - start:4.0f}s  {spec.container}: running={st['running']} "
                    f"paused={st['paused']}  journal entries={len(left)}")
            if not left and st["running"] and not st["paused"]:
                break
            time.sleep(ctx.settings.timings.poll_s * 2)
        ok = not ctx.journal.entries() and not ctx.docker.state(spec.container)["paused"]
        if ok:
            ctx.docker.wait_healthy(spec.container, ctx.settings.timings.healthy_timeout_s)
            AssetTracker(ctx.journal.state_dir / "probe_assets.json").cleanup(ctx.admin(fresh=True))
        ctx.log(f"GUARD DRILL {'PASSED: the guard restored the system' if ok else 'FAILED'}")
        return 0 if ok else 1
    finally:
        ctx.close()


# ------------------------------------------------------------------------ discover / run / report / list / seed
def cmd_discover(settings: Settings, args: Namespace) -> int:
    """Write topology.json + topology.md (Mermaid) for the current sandbox."""
    from .topology import discover, to_mermaid

    ctx = _ctx(settings)
    try:
        run_dir = ctx.new_run_dir("discover")
        topo = discover(ctx)
        topo["mermaid"] = to_mermaid(topo)
        write_json(run_dir / "topology.json", topo)
        (run_dir / "topology.md").write_text("# Dependency map\n\n```mermaid\n" + topo["mermaid"] + "\n```\n",
                                             encoding="utf-8")
        for e in topo["edges"]:
            print(f"  {e['from']:>8} -> {e['to']:<10} [{e['source']}] {e['label']}")
        print("Not declared in compose: " + "; ".join(topo["undeclared_dependencies"]))
        print(f"\n{topo['mermaid']}\n\nsaved {run_dir / 'topology.md'}")
        return 0
    finally:
        ctx.close()


def cmd_run(settings: Settings, args: Namespace) -> int:
    """Full workflow (see workflow.py)."""
    from .faults import DEFAULT_EXPERIMENTS
    from .workflow import Workflow

    experiments = [x.strip() for x in args.experiments.split(",") if x.strip()] or DEFAULT_EXPERIMENTS
    for fault_id in experiments:
        get_fault(fault_id)  # reject unknown faults before touching anything
    ctx = _ctx(settings)
    try:
        wf = Workflow(ctx, experiments, suggest=not args.no_suggestions)
        run_dir = wf.run()
        print(f"\nstatus: {wf.status}   overall score: "
              f"{json.loads((run_dir / 'findings.json').read_text(encoding='utf-8')).get('overall_score')}")
        print(f"report: {run_dir / 'report.html'}")
        return 0 if wf.status == "completed" else 1
    finally:
        ctx.close()


def cmd_report(settings: Settings, args: Namespace) -> int:
    from pathlib import Path

    from .report import render_reports

    run_dir = Path(args.run)
    if not run_dir.exists():
        run_dir = settings.runs_dir / args.run
    if args.reanalyze:
        reanalyze(settings, run_dir)
    md, html_path = render_reports(run_dir)
    print(f"wrote {md}\nwrote {html_path}")
    return 0


def reanalyze(settings: Settings, run_dir) -> None:
    """Re-run only the AI text (Analyst + Reporter) on a run's saved evidence.

    Touches neither Docker nor Immich. Measurements, scores, heatmaps and fix suggestions are
    kept exactly as recorded; only the explanations are regenerated (with the grounding check).
    """
    from . import agents
    from .context import Console
    from .llm import LLMClient
    from .report import MANUAL_FINDINGS
    from .scoring import overall_score
    from .topology import prediction_scorecard

    console = Console(settings.secrets())
    path = run_dir / "findings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    llm = LLMClient(settings, console.info)
    full = []
    for exp in data["experiments"]:
        evidence = json.loads((run_dir / "evidence" / exp["label"] / "evidence.json").read_text(encoding="utf-8"))
        merged = {**exp, **{k: evidence[k] for k in ("fault_results", "recovery_results", "log_tails") if k in evidence}}
        exp["analysis"] = agents.analyze(llm, merged, data["topology"])
        console.info(f"{exp['label']}: analysis source {exp['analysis']['source']}")
        full.append({**merged, "analysis": exp["analysis"]})
    data["report"] = agents.write_report(llm, full, overall_score(full), prediction_scorecard(full), MANUAL_FINDINGS)
    data["llm_stats"] = {**llm.stats, "api_calls": llm.calls, "reanalyzed": True}
    console.info(f"report source {data['report']['source']}; LLM stats {data['llm_stats']}")
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def cmd_list(settings: Settings, args: Namespace) -> int:
    from .faults import CATALOG, DEFAULT_EXPERIMENTS
    from .fixes import KNOBS

    print("Fault catalog (anything else is rejected):")
    for f in CATALOG.values():
        default = "default" if f.id in DEFAULT_EXPERIMENTS else ("optional" if f.optional else "")
        print(f"  {f.id:<16} {f.container:<25} {f.kind:<8} likelihood {f.likelihood}  {default:<8} {f.description}")
    print("\nSettings a fix suggestion may use (anything else is flagged; ShutterStorm never applies fixes):")
    for part, knobs in KNOBS.items():
        for name, rule in knobs.items():
            print(f"  {part}: {name}\n      {rule}")
    return 0


def cmd_seed(settings: Settings, args: Namespace) -> int:
    """Upload a folder of images as test content (Immich skips duplicates by checksum)."""
    ctx = _ctx(settings)
    try:
        client = ctx.admin()
        exts = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
        # "._name.jpg" files are macOS metadata (from zips made on a Mac), not images.
        files = sorted(p for p in args.dir.rglob("*") if p.suffix.lower() in exts and not p.name.startswith("._"))
        if args.limit:
            files = files[:args.limit]
        created = dup = failed = 0
        latencies: list[float] = []
        total_bytes = 0
        start = time.perf_counter()
        for i, path in enumerate(files, 1):
            data = path.read_bytes()
            t0 = time.perf_counter()
            resp = client.upload(data, path.name, timeout=60)
            latencies.append((time.perf_counter() - t0) * 1000)
            total_bytes += len(data)
            created += resp.status_code == 201
            dup += resp.status_code == 200
            failed += resp.status_code not in (200, 201)
            if i % 20 == 0 or i == len(files):
                ctx.log(f"  {i}/{len(files)} uploaded ({time.perf_counter() - start:.1f}s)")
        elapsed = time.perf_counter() - start
        from .probes import percentile

        ctx.log(f"seeded {args.dir}: {created} new, {dup} duplicates skipped, {failed} failed")
        if files:
            ctx.log(f"upload time: {elapsed:.1f}s total for {len(files)} files ({total_bytes / 1e6:.1f} MB), "
                    f"{len(files) / elapsed:.1f} files/s, per file p50 {percentile(latencies, 50):.0f} ms, "
                    f"p95 {percentile(latencies, 95):.0f} ms")
        if args.wait and files:
            wait_for_queues(ctx, client, start)
        return 0 if not failed else 1
    finally:
        ctx.close()


def wait_for_queues(ctx: Ctx, client, upload_start: float, limit_s: float = 1800) -> None:
    """Poll GET /api/queues until no job is waiting or active (thumbnails, ML, OCR...)."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit_s:
        busy = {q["name"]: q["statistics"]["waiting"] + q["statistics"]["active"] for q in client.queues()}
        busy = {k: v for k, v in busy.items() if v}
        if not busy:
            ctx.log(f"all background jobs done: {time.perf_counter() - t0:.1f}s after the last upload, "
                    f"{time.perf_counter() - upload_start:.1f}s after the first upload")
            return
        ctx.log("  still processing: " + ", ".join(f"{k}={v}" for k, v in sorted(busy.items())))
        time.sleep(5)
    ctx.log(f"queues not empty after {limit_s:.0f}s; stopped waiting")


def cmd_dashboard(settings: Settings, args: Namespace) -> int:
    from .dashboard import serve

    serve(settings, args.port)
    return 0
