"""The workflow state machine.

    check environment, scope, clean checkpoint
      -> discover dependency map
      -> baseline tests
      -> for each fault:
           predict -> inject (journal + TTL + guard) -> observe -> undo + verify
           -> recovery + integrity -> score (math) -> explain (Analyst)
           -> suggest a fix (Fix Advisor) -> check the suggestion against the allowed settings
      -> restore test environment + verify cleanup   (always: also on error, STOP, Ctrl+C)
      -> report

ShutterStorm only injects faults and undoes them. It never applies a fix: suggestions are
shown in the CLI and in the report for a human to decide on. Control flow, injection, undo,
scoring and cleanup are deterministic code; LLMs are used only in agents.predict / analyze /
suggest_fix / write_report.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from . import agents
from .checkpoint import preflight_problems, take_checkpoint, verify_cleanup
from .context import Ctx, write_json
from .experiment import run_experiment
from .faults import FaultSpec, get_fault, undo_entry
from .fixes import describe_patch, manual_steps, ml_image, normalize_patch, validate_patch
from .llm import LLMClient
from .probes import ALL_JOURNEYS, AssetTracker, Prober, ProbeResult, summarize
from .report import MANUAL_FINDINGS, render_reports
from .reporting_io import append_probes_csv, summary_table
from .safety import AbortRun, check_stop, panic
from .scoring import classify_cell, overall_score, score_experiment
from .topology import (JOURNEY_DEPS, add_empirical_edges, discover, predict_impact, prediction_scorecard,
                       score_prediction, to_mermaid)

RAW_KEYS = ("fault_results", "recovery_results")


class Workflow:
    """Runs the full chaos workflow once and writes everything into ``runs/<id>/``."""

    def __init__(self, ctx: Ctx, experiments: list[str], suggest: bool = True) -> None:
        self.ctx = ctx
        self.specs: list[FaultSpec] = [get_fault(f) for f in experiments]  # rejects unknown faults up front
        self.suggest = suggest
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
        self.cleanup: dict = {}

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
                self.experiments.append(exp)
                if self.suggest and exp["actual_impacted"]:
                    exp["suggestion"] = self.suggest_fix(spec, exp)
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
    def run_one(self, spec: FaultSpec) -> dict:
        self.step(f"Inject approved fault: {spec.id}")
        deterministic = predict_impact(spec, self.topology)
        prediction = agents.predict(self.llm, spec, self.topology, deterministic)
        self.ctx.log(f"   predicted impact ({prediction['source']}): {prediction['predicted_impacted']}")
        ev = run_experiment(self.ctx, self.prober, spec, self.run_dir, on_round=self._on_round)
        exp = self.build_record(ev, spec, prediction)
        self.step(f"Confirm failure and capture evidence: {spec.id}")
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

    # ---------------------------------------------------------------- fix suggestion
    def suggest_fix(self, spec: FaultSpec, exp: dict) -> dict:
        """Ask the Fix Advisor for one suggestion, check it, print it. Nothing is applied."""
        ctx = self.ctx
        self.step(f"Suggest a fix (not applied): {spec.id}")
        config = ctx.admin().get_system_config()
        current = {"machineLearning": {k: config["machineLearning"][k] for k in ("urls", "availabilityChecks")},
                   "job": {q: config["job"][q] for q in config["job"]}}
        proposal = agents.suggest_fix(self.llm, exp, current)
        code_fix = agents.CODE_LEVEL_FIXES.get(spec.service, "")
        if proposal.get("no_fix_possible"):
            suggestion = {"kind": "code", "source": proposal["source"], "recommendation": proposal["recommendation"],
                          "template_code_fix": code_fix, "summary": proposal["recommendation"]}
        else:
            image = ml_image(ctx)
            patch = normalize_patch(proposal.get("patch", {}), image)
            errors = validate_patch(patch, image)
            suggestion = {"kind": "config", "source": proposal["source"], "rationale": proposal.get("rationale"),
                          "expected_effect": proposal.get("expected_effect"), "patch": patch,
                          "valid": not errors, "validation_errors": errors, "diff": describe_patch(patch, config),
                          "manual_steps": manual_steps(patch) if not errors else [],
                          "template_code_fix": code_fix,
                          "summary": (proposal.get("rationale") or "") if not errors
                          else f"(config suggestion rejected: {'; '.join(errors)}) {code_fix}"}
        suggestion["status"] = "suggestion only: not applied, not tested"
        self.print_suggestion(spec, exp, suggestion)
        return suggestion

    def print_suggestion(self, spec: FaultSpec, exp: dict, s: dict) -> None:
        """Show the issue and the suggested fix in the CLI."""
        who = {"live": "AI (live)", "cache": "AI (cached answer)"}.get(s["source"], "template (no AI)")
        broken = ", ".join(f"{j}={c}" for j, c in exp["heatmap"].items() if c not in ("ok", "n/a"))
        lines = [f"   ---- SUGGESTED FIX for {spec.id} (not applied, not tested) ----",
                 f"   issue: {broken}; score {exp['scoring']['score']}, severity {exp['scoring']['severity_label']}",
                 f"   suggested by: {who}"]
        if s["kind"] == "config":
            lines.append(f"   why: {s.get('rationale')}")
            lines += ["      " + line for line in s["diff"].splitlines()]
            lines.append("   uses allowed settings only: "
                         + ("yes" if s["valid"] else "NO - " + "; ".join(s["validation_errors"])))
        else:
            lines.append(f"   no config-level fix; recommendation: {s['recommendation']}")
        lines.append(f"   code-level fix (template): {s['template_code_fix']}")
        self.ctx.log("\n".join(lines))

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
                cleanup = verify_cleanup(ctx, self.checkpoint, self.prober.tracker if self.prober else None)
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
            "suggestions_enabled": self.suggest,
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
            data["cleanup"] = self.cleanup
            data["llm_stats"] = {**self.llm.stats, "api_calls": self.llm.calls}
        write_json(self.run_dir / "findings.json", data)


def summarize_dicts(results: list[dict]) -> dict:
    """``probes.summarize`` for results stored as dicts."""
    return summarize([ProbeResult(**r) for r in results])
