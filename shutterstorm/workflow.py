"""The workflow state machine (the team's agreed design).

    check environment, scope, clean checkpoint
      -> baseline tests
      -> for each approved fault:
           predict -> inject -> confirm failure + capture evidence -> analyse
           [--repair] agent proposes patch -> validate -> apply -> changed-files check
                      -> protected tests -> SAME fault re-injected
                      -> pass: save verified patch | fail + retries left: feed back | exhausted: unresolved
      -> restore test environment + verify cleanup   (always: also on error, STOP, Ctrl+C)
      -> report

Control flow, injection, validation, retries and cleanup are deterministic code.
LLMs are used only in agents.predict/analyze/propose_repair/write_report.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from . import agents
from .checkpoint import changed_files, hash_dir, preflight_problems, take_checkpoint, verify_cleanup
from .config import ML2, OVERRIDE_FILE
from .context import Ctx, write_json
from .experiment import run_experiment
from .faults import FaultSpec, get_fault, undo_entry
from .llm import LLMClient
from .probes import ALL_JOURNEYS, PROTECTED_JOURNEYS, AssetTracker, Prober, summarize
from .report import MANUAL_FINDINGS, render_reports
from .reporting_io import append_probes_csv, summary_table
from .repair import (apply_patch, describe_patch, forget_patch, ml_image, normalize_patch, revert_patch,
                     validate_patch)
from .safety import AbortRun, check_stop, panic
from .scoring import classify_cell, overall_score, score_experiment
from .topology import (JOURNEY_DEPS, add_empirical_edges, discover, predict_impact, prediction_scorecard,
                       score_prediction, to_mermaid)

CELL_RANK = {"hung": 0, "fast-fail": 1, "slow": 2, "ok": 3, "n/a": 3}
MIN_SCORE_GAIN = 10.0
RAW_KEYS = ("fault_results", "recovery_results")


class Workflow:
    """Runs the full chaos workflow once and writes everything into ``runs/<id>/``."""

    def __init__(self, ctx: Ctx, experiments: list[str], repair: bool = False, keep_fix: bool = False) -> None:
        self.ctx = ctx
        self.specs: list[FaultSpec] = [get_fault(f) for f in experiments]  # rejects unknown faults up front
        self.repair = repair
        self.keep_fix = keep_fix
        self.llm = LLMClient(ctx.settings, ctx.log)
        self.run_dir: Path | None = None
        self.prober: Prober | None = None
        self.checkpoint: dict | None = None
        self.topology: dict = {}
        self.baseline: list[dict] = []
        self.experiments: list[dict] = []
        self.status = "running"
        self.error = ""
        self.steps: list[dict] = []

    # ---------------------------------------------------------------- helpers
    def step(self, name: str) -> None:
        check_stop(self.ctx)
        self.steps.append({"step": name, "t": time.time()})
        self.ctx.log(f"== {name}")

    def _on_round(self, phase: str, n: int, results) -> None:
        self.ctx.log(f"   {phase:<8} #{n}: " + " ".join(f"{r.journey}={r.outcome}" for r in results))

    # ---------------------------------------------------------------- main
    def run(self) -> Path:
        ctx = self.ctx
        self.run_dir = ctx.new_run_dir("run")
        self.started = time.time()
        ctx.journal.clear_stop()
        try:
            self.step("Check environment, scope and clean checkpoint")
            problems = preflight_problems(ctx)
            if problems:
                raise AbortRun("environment not clean: " + "; ".join(problems))
            self.checkpoint = take_checkpoint(ctx, self.run_dir)
            self.topology = discover(ctx)
            write_json(self.run_dir / "topology.json", self.topology)
            ctx.log(f"   undeclared dependencies: {self.topology['undeclared_dependencies']}")

            self.step("Baseline tests")
            self.prober = Prober(ctx.settings, AssetTracker(ctx.journal.state_dir / "probe_assets.json"),
                                 self.run_dir.name)
            self.prober.authenticate()
            self.prober.warmup()
            self.prober.experiment = "baseline"
            results = []
            for i in range(ctx.settings.timings.baseline_rounds):
                r = self.prober.run_round("baseline", async_deadline=ctx.settings.timings.async_deadline_s)
                self._on_round("baseline", i + 1, r)
                results += r
            append_probes_csv(self.run_dir / "probes.csv", results)
            self.baseline = [r.to_dict() for r in results]
            print(summary_table(results))
            if not all(r.ok for r in results):
                raise AbortRun("baseline is not healthy; refusing to inject faults")

            for spec in self.specs:
                exp = self.run_one(spec)
                if self.repair and exp["actual_impacted"]:
                    exp["repair"] = self.repair_loop(spec, exp)
                self.experiments.append(exp)
                self._save_findings()
            self.status = "completed"
        except AbortRun as exc:
            self.status, self.error = "aborted", str(exc)
            ctx.log(f"ABORTED: {exc}")
            if not str(exc).startswith("environment not clean"):
                panic(ctx, reason=str(exc))
        except KeyboardInterrupt:
            self.status, self.error = "interrupted", "Ctrl+C"
            ctx.log("Ctrl+C: restoring the test environment")
        except Exception as exc:  # noqa: BLE001 - always fall through to cleanup
            self.status, self.error = "error", f"{exc.__class__.__name__}: {exc}"
            ctx.log(f"ERROR: {self.error}")
            (self.run_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            self.cleanup_and_report()
        return self.run_dir

    # ---------------------------------------------------------------- one experiment
    def run_one(self, spec: FaultSpec, label: str | None = None) -> dict:
        label = label or spec.id
        self.step(f"Inject approved fault: {label}")
        deterministic = predict_impact(spec, self.topology)
        prediction = agents.predict(self.llm, spec, self.topology, deterministic)
        self.ctx.log(f"   predicted impact ({prediction['source']}): {prediction['predicted_impacted']}")
        ev = run_experiment(self.ctx, self.prober, spec, self.run_dir, label=label, on_round=self._on_round)
        exp = self.build_record(ev, spec, prediction)
        self.step(f"Confirm failure and capture evidence: {label}")
        self.ctx.log(f"   heatmap: {exp['heatmap']}  score {exp['scoring']['score']}  "
                     f"severity {exp['scoring']['severity']} ({exp['scoring']['severity_label']})")
        exp["analysis"] = agents.analyze(self.llm, exp, self.topology)
        self.ctx.log(f"   analysis ({exp['analysis']['source']}): {exp['analysis']['summary'][:200]}")
        return exp

    def build_record(self, ev: dict, spec: FaultSpec, prediction: dict) -> dict:
        heatmap = {j: classify_cell(ev["fault_results"], self.baseline, j) for j in ALL_JOURNEYS}
        for downstream in ("J5", "J6"):  # no asset to check because the upload itself failed
            if heatmap[downstream] == "n/a" and heatmap["J4"] in ("hung", "fast-fail"):
                heatmap[downstream] = heatmap["J4"]
        actual = [j for j, c in heatmap.items() if c not in ("ok", "n/a")]
        return {
            **ev,
            "kind": spec.kind,
            "prediction": prediction,
            "heatmap": heatmap,
            "actual_impacted": actual,
            "prediction_check": score_prediction(prediction["predicted_impacted"], actual),
            "scoring": score_experiment(ev, self.baseline, JOURNEY_DEPS, spec.kind),
            "baseline_summary": summarize_dicts(self.baseline),
        }

    # ---------------------------------------------------------------- repair loop
    def repair_loop(self, spec: FaultSpec, exp: dict) -> dict:
        ctx = self.ctx
        attempts: list[dict] = []
        image = ml_image(ctx)
        for n in range(1, ctx.settings.max_repair_attempts + 1):
            self.step(f"Agent repairs within allowed scope: {spec.id} attempt {n}")
            config = ctx.admin(fresh=True).get_system_config()
            current = {"machineLearning": {k: config["machineLearning"][k] for k in ("urls", "availabilityChecks")},
                       "job": {q: config["job"][q] for q in config["job"]},
                       "ml2_running": ctx.docker.state(ML2)["running"]}
            proposal = agents.propose_repair(self.llm, exp, current, attempts)
            if proposal.get("no_fix_possible"):
                ctx.log(f"   repair agent ({proposal['source']}): no config-level fix -> {proposal['recommendation'][:160]}")
                return {"status": "no_fix_possible", "recommendation": proposal["recommendation"],
                        "source": proposal["source"], "attempts": attempts}
            patch = normalize_patch(proposal.get("patch", {}), image)
            errors = validate_patch(patch, image, ml2_present=ctx.docker.state(ML2)["exists"])
            attempt = {"n": n, "rationale": proposal.get("rationale"), "expected_effect": proposal.get("expected_effect"),
                       "patch": patch, "diff": describe_patch(patch, config), "source": proposal["source"]}
            if errors:
                ctx.log(f"   patch REJECTED by validator: {errors}")
                attempts.append({**attempt, "result": "rejected", "errors": errors})
                continue
            ctx.log("   patch:\n" + "\n".join("      " + line for line in attempt["diff"].splitlines()))
            attempt.update(self.test_patch(spec, exp, patch, n))
            attempts.append(attempt)
            self._save_findings()
            if attempt["result"] == "passed":
                self.step(f"Save verified patch and report: {spec.id}")
                write_json(self.run_dir / "patches" / f"{spec.id}.json", attempt)
                (self.run_dir / "patches" / f"{spec.id}.diff").write_text(attempt["diff"] + "\n", encoding="utf-8")
                return {"status": "verified", "attempts": attempts, "verified_attempt": n,
                        "recommendation": f"Apply: {proposal.get('rationale', '')}", "source": proposal["source"],
                        "kept": self.keep_fix}
            ctx.log(f"   attempt {n} failed: {attempt['reasons']}")
        self.step(f"Save unresolved result: {spec.id}")
        return {"status": "unresolved", "attempts": attempts,
                "recommendation": agents.CODE_LEVEL_FIXES.get(spec.service, ""), "source": "fallback"}

    def test_patch(self, spec: FaultSpec, exp: dict, patch: dict, n: int) -> dict:
        """Apply -> changed-files check -> protected tests -> same fault -> compare -> revert."""
        ctx = self.ctx
        files_before = hash_dir(Path(ctx.settings.immich_dir))
        entry_ids: list[str] = []
        passed = False
        result: dict = {}
        try:
            entry_ids = apply_patch(ctx, patch, f"repair-{spec.id}-{n}")
            self.step(f"Check changed files and run protected tests: {spec.id} attempt {n}")
            changed = changed_files(files_before, hash_dir(Path(ctx.settings.immich_dir)))
            files_ok = all(p == OVERRIDE_FILE for p in changed)
            self.prober.experiment = f"{spec.id}-repair{n}-protected"
            protected = []
            for i in range(ctx.settings.timings.protected_rounds):
                r = self.prober.run_round("protected", ALL_JOURNEYS, async_deadline=ctx.settings.timings.async_deadline_s)
                self._on_round("protected", i + 1, r)
                protected += r
            append_probes_csv(self.run_dir / "probes.csv", protected)
            protected_failures = sorted({r.journey for r in protected if r.journey in PROTECTED_JOURNEYS and not r.ok})
            retest_ev = run_experiment(ctx, self.prober, spec, self.run_dir, label=f"{spec.id}-repair{n}",
                                       on_round=self._on_round)
            retest = self.build_record(retest_ev, spec, exp["prediction"])
            delta = retest["scoring"]["score"] - exp["scoring"]["score"]
            improved = [j for j in exp["actual_impacted"] if CELL_RANK[retest["heatmap"][j]] > CELL_RANK[exp["heatmap"][j]]]
            worse = [j for j in ALL_JOURNEYS if CELL_RANK[retest["heatmap"][j]] < CELL_RANK[exp["heatmap"][j]]]
            reasons = []
            if not files_ok:
                reasons.append(f"unexpected file changes in IMMICH_DIR: {changed}")
            if protected_failures:
                reasons.append(f"protected journeys regressed: {protected_failures}")
            if not improved:
                reasons.append("no target journey improved")
            if worse:
                reasons.append(f"journeys got worse: {worse}")
            if delta < MIN_SCORE_GAIN:
                reasons.append(f"score gain {delta:+.1f} < {MIN_SCORE_GAIN}")
            if not retest_ev["undo"]["ok"]:
                reasons.append("fault undo not verified")
            passed = not reasons
            ctx.log(f"   re-test: heatmap {retest['heatmap']} score {retest['scoring']['score']} ({delta:+.1f})")
            result = {"result": "passed" if passed else "failed", "reasons": reasons, "changed_files": changed,
                      "protected_failures": protected_failures, "retest_heatmap": retest["heatmap"],
                      "retest_score": retest["scoring"]["score"], "score_delta": round(delta, 1),
                      "improved_journeys": improved, "retest_summary": retest["fault_summary"]}
        except AbortRun:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed patch is a failed attempt, not a crash
            ctx.log(f"   patch application/test failed: {exc}")
            result = {"result": "failed", "reasons": [f"{exc.__class__.__name__}: {exc}"]}
        finally:
            if passed and self.keep_fix:
                forget_patch(ctx, entry_ids)
                ctx.log("   --keep-fix: verified patch left applied")
            elif entry_ids:
                ok, details = revert_patch(ctx, entry_ids)
                if not ok:
                    raise AbortRun(f"patch revert could not be verified: {details}")
        return result

    # ---------------------------------------------------------------- cleanup + report
    def cleanup_and_report(self) -> None:
        ctx = self.ctx
        cleanup = {"ok": False, "checks": [], "note": "no checkpoint was taken"}
        try:
            self.steps.append({"step": "Restore test environment and verify cleanup", "t": time.time()})
            ctx.log("== Restore test environment and verify cleanup")
            for entry in ctx.journal.entries():
                undo_entry(ctx, entry, who="cleanup")
            if self.checkpoint is not None:
                cleanup = verify_cleanup(ctx, self.checkpoint, self.prober.tracker if self.prober else None,
                                         keep_fix=self.keep_fix)
        except Exception as exc:  # noqa: BLE001
            cleanup = {"ok": False, "checks": [], "note": f"cleanup error: {exc}"}
        write_json(self.run_dir / "cleanup.json", cleanup)
        for c in cleanup["checks"]:
            if not c["ok"]:
                ctx.log(f"   cleanup FAILED: {c['name']} ({c['detail']})")
        ctx.log(f"   cleanup verification: {'PASS' if cleanup['ok'] else 'FAIL'}")
        if self.status == "completed" and not cleanup["ok"]:
            self.status = "completed (cleanup failed)"
        self.cleanup = cleanup
        try:
            self._save_findings(final=True)
            render_reports(self.run_dir)
            ctx.log(f"report: {self.run_dir / 'report.html'}")
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"report generation failed: {exc}")
            (self.run_dir / "report_error.txt").write_text(traceback.format_exc(), encoding="utf-8")

    def _save_findings(self, final: bool = False) -> None:
        exps = [{k: v for k, v in e.items() if k not in RAW_KEYS} for e in self.experiments]
        data = {
            "run_id": self.run_dir.name,
            "simulated": self.ctx.simulate,
            "status": self.status,
            "error": self.error,
            "started": self.started,
            "finished": time.time(),
            "llm_mode": self.ctx.settings.llm_mode,
            "repair_enabled": self.repair,
            "steps": self.steps,
            "baseline_summary": summarize_dicts(self.baseline),
            "topology": self.topology,
            "experiments": exps,
            "overall_score": overall_score(self.experiments),
        }
        if final:
            if self.topology:
                add_empirical_edges(self.topology, self.experiments)
                self.topology["mermaid"] = to_mermaid(self.topology)
                write_json(self.run_dir / "topology.json", self.topology)
                (self.run_dir / "topology.md").write_text(
                    "# Dependency map\n\n```mermaid\n" + self.topology["mermaid"] + "\n```\n", encoding="utf-8")
            data["scorecard"] = prediction_scorecard(self.experiments) if self.experiments else {}
            data["report"] = agents.write_report(self.llm, self.experiments, data["overall_score"],
                                                 data["scorecard"], MANUAL_FINDINGS) if self.experiments else {}
            data["cleanup"] = getattr(self, "cleanup", {})
            data["llm_stats"] = {**self.llm.stats, "api_calls": self.llm.calls}
        write_json(self.run_dir / "findings.json", data)


def summarize_dicts(results: list[dict]) -> dict:
    """``probes.summarize`` for results stored as dicts."""
    from .probes import ProbeResult

    return summarize([ProbeResult(**r) for r in results])

