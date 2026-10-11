"""The workflow state machine.

    check environment, scope, clean checkpoint
      -> discover dependency map
      -> AI experiment planner: tentative plan from the map (unless --experiments is given)
      -> baseline tests
      -> repeat up to the budget:
           AI decides the NEXT experiment from what this run has shown so far (what + when),
           with its strength, duration and safety limits (how safely; clamped, stricter-only)
           -> predict -> inject (journal + TTL + guard) -> observe (threshold monitor: auto-rollback)
           -> undo + verify
           -> recovery + integrity -> score (math) -> explain (Analyst)
           -> suggest a fix (Fix Advisor) -> check the suggestion against the allowed settings
      -> restore test environment + verify cleanup   (always: also on error, STOP, Ctrl+C)
      -> report

ShutterStorm only injects faults and undoes them. It never applies a fix: suggestions are
shown in the CLI and in the report for a human to decide on. Control flow, injection, undo,
scoring and cleanup are deterministic code; LLMs are used only through ``self.agents``
(Planner, Analyst, Fix Advisor, Reporter), in-process or in the isolated agents container
(``AGENTS_URL``); the controller re-checks every answer before acting on it.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from . import agents, planner
from .checkpoint import preflight_problems, take_checkpoint, verify_cleanup
from .compare import compare_runs
from .config import Thresholds
from .context import Ctx, write_json
from .experiment import run_experiment
from .faults import FaultSpec, get_fault, undo_entry
from .fixes import describe_patch, manual_steps, ml_image, normalize_patch, validate_patch
from .agentclient import Agents
from .probes import ALL_JOURNEYS, AssetTracker, Prober, ProbeResult, summarize
from .report import MANUAL_FINDINGS, render_reports
from .events import round_results
from .reporting_io import append_probes_csv, summary_table
from .safety import AbortRun, check_stop, panic
from .scoring import classify_cell, overall_score, score_experiment
from .topology import (JOURNEY_DEPS, add_empirical_edges, discover, predict_impact, prediction_scorecard,
                       score_prediction, to_mermaid)

RAW_KEYS = ("fault_results", "recovery_results")


class Workflow:
    """Runs the full chaos workflow once and writes everything into ``runs/<id>/``."""

    def __init__(self, ctx: Ctx, experiments: list[str] | None = None, suggest: bool = True,
                 budget: int = planner.DEFAULT_BUDGET, thresholds: Thresholds | None = None,
                 compare_to: str | None = None) -> None:
        self.ctx = ctx
        # Explicit experiments skip the planner; unknown faults are rejected up front.
        self.specs: list[FaultSpec] = [get_fault(f) for f in experiments or []]
        self.use_planner = not self.specs
        self.budget = budget
        self.thresholds = thresholds or ctx.settings.thresholds
        self.compare_to = compare_to
        self.plan: dict = {}
        self.suggest = suggest
        self.agents = Agents(ctx.settings, ctx.log)
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
        self.ctx.event("step", name=name)

    def _on_round(self, phase: str, n: int, results) -> None:
        self.ctx.log(f"   {phase:<8} #{n}: " + " ".join(f"{r.journey}={r.outcome}" for r in results))

    # ---------------------------------------------------------------- main
    def run(self) -> Path:
        ctx = self.ctx
        self.run_dir = ctx.new_run_dir("run")
        self.agents.run_id = self.run_dir.name
        ctx.event("run_started", run_id=self.run_dir.name, kind="run", simulated=ctx.simulate,
                  budget=self.budget if self.use_planner else len(self.specs), planner=self.use_planner,
                  caps={"max_error_rate": self.thresholds.max_error_rate, "max_p95_ms": self.thresholds.max_p95_ms},
                  agents=self.agents.where)
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

            self.step("AI experiment planner" if self.use_planner else "Experiments chosen by the operator")
            self.make_plan()

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
                ctx.event("baseline_round", round=i + 1, results=round_results(r))
                results += r
            append_probes_csv(self.run_dir / "probes.csv", results)
            self.baseline = [r.to_dict() for r in results]
            print(summary_table(results))
            if not all(r.ok for r in results):
                raise AbortRun("baseline is not healthy; refusing to inject faults")

            step = 0
            while True:
                decision = self.next_decision(step)
                if decision is None:
                    break
                step += 1
                spec = get_fault(decision["fault"])
                exp = self.run_one(spec, decision)
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
    def make_plan(self) -> None:
        ctx = self.ctx
        if self.use_planner:
            self.plan = planner.plan(ctx, self.agents, self.topology, self.budget)
            self.specs = planner.spec_list(self.plan)
        else:
            self.plan = {"source": "operator", "strategy": "experiments given with --experiments", "items": [
                {"rank": n, "fault": s.id, "reason": "chosen by the operator", "failure_class": planner.FAILURE_CLASS[s.kind]}
                for n, s in enumerate(self.specs, 1)], "skipped": []}
        write_json(self.run_dir / "plan.json", self.plan)
        ctx.event("plan", source=self.plan["source"], strategy=self.plan.get("strategy", ""),
                  items=[{k: i.get(k) for k in ("rank", "fault", "failure_class", "reason")} for i in self.plan["items"]])
        who = {"live": "AI planner (live)", "cache": "AI planner (cached)", "fallback": "deterministic planner",
               "operator": "operator"}[self.plan["source"]]
        ctx.log(f"   plan by {who}: {self.plan.get('strategy', '')}")
        for item in self.plan["items"]:
            ctx.log(f"   {item['rank']}. {item['fault']:<17} [{item['failure_class']}] {item['reason']}")
        ctx.log(f"   safety caps (the AI may only go stricter): {self.thresholds.describe()}")

    def next_decision(self, step: int) -> dict | None:
        """What to run next, now, and how safely: the AI planner, or the operator's list."""
        ctx = self.ctx
        if self.use_planner:
            if step >= self.budget:
                return None
            self.step(f"AI decides experiment {step + 1} of up to {self.budget}")
            decision = planner.decide_next(ctx, self.agents, self.plan, self.experiments, self.thresholds, self.budget)
        elif step < len(self.specs):
            decision = planner.operator_decision(self.specs[step], self.thresholds, ctx.settings.max_fault_seconds,
                                                 step + 1, ctx.settings.timings.fault_window_s)
        else:
            decision = None
        if decision is None:
            return None
        who = {"live": "AI (live)", "cache": "AI (cached)", "fallback": "deterministic fallback",
               "operator": "operator"}[decision["source"]]
        change = "" if decision["followed_plan"] else f" (changed plan: tentative next was {decision['plan_next']})"
        ctx.log(f"   decision by {who}: {decision['fault']}{change} - {decision['reason']}")
        ctx.log(f"   strength/duration: {decision['settings']}; safety: {decision['safety']} - {decision['safety_reason']}")
        for note in decision["clamped"]:
            ctx.log(f"   clamped by the hard bounds: {note}")
        self.plan.setdefault("decisions", []).append(planner.decision_record(decision))
        ctx.event("decision", **planner.decision_record(decision))
        write_json(self.run_dir / "plan.json", self.plan)
        return decision

    def run_one(self, spec: FaultSpec, decision: dict) -> dict:
        self.step(f"Inject approved fault: {spec.id}")
        deterministic = predict_impact(spec, self.topology)
        prediction = self.agents.predict(spec, self.topology, deterministic)
        self.ctx.log(f"   predicted impact ({prediction['source']}): {prediction['predicted_impacted']}")
        self.ctx.event("experiment_started", fault=spec.id, parts=[p.id for p in spec.atomic], params=decision["params"],
                       duration_s=decision["duration_s"], predicted=prediction["predicted_impacted"],
                       limits={"max_error_rate": decision["thresholds"].max_error_rate,
                               "max_p95_ms": decision["thresholds"].max_p95_ms})
        ev = run_experiment(self.ctx, self.prober, spec, self.run_dir, on_round=self._on_round,
                            thresholds=decision["thresholds"], window_s=decision["duration_s"],
                            params=decision["params"])
        exp = self.build_record(ev, spec, prediction)
        exp["decision"] = planner.decision_record(decision)
        self.step(f"Confirm failure and capture evidence: {spec.id}")
        self.ctx.log(f"   heatmap: {exp['heatmap']}  score {exp['scoring']['score']}  "
                     f"severity {exp['scoring']['severity']} ({exp['scoring']['severity_label']})")
        self.ctx.event("experiment_done", fault=spec.id, score=exp["scoring"]["score"],
                       severity=exp["scoring"]["severity"], severity_label=exp["scoring"]["severity_label"],
                       heatmap=exp["heatmap"], auto_stopped=exp.get("auto_stopped"), recovery_s=exp.get("recovery_s"))
        exp["analysis"] = self.agents.analyze(exp, self.topology)
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
        proposal = self.agents.suggest_fix(exp, current)
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
        ctx.event("cleanup", ok=cleanup["ok"], checks=len(cleanup.get("checks", [])),
                  failed=[c["name"] for c in cleanup.get("checks", []) if not c["ok"]], note=cleanup.get("note", ""))
        if self.status == "completed" and not cleanup["ok"]:
            self.status = "completed (cleanup failed)"
        self.cleanup = cleanup
        try:
            self._save_findings(final=True)
            render_reports(self.run_dir)
            ctx.log(f"report: {self.run_dir / 'report.html'}")
            ctx.event("run_finished", status=self.status, error=self.error,
                      overall_score=overall_score(self.experiments))
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
            "plan": {k: v for k, v in self.plan.items() if k != "candidates"},
            "thresholds": self.thresholds.describe(),
            "decisions": self.plan.get("decisions", []),
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
            data["report"] = self.agents.write_report(self.experiments, data["overall_score"],
                                                 data["scorecard"], MANUAL_FINDINGS) if self.experiments else {}
            data["cleanup"] = self.cleanup
            data["llm_stats"] = {**self.agents.stats, "agents": self.agents.where}
            if self.compare_to and self.experiments:
                try:
                    data["comparison"] = compare_runs(self.ctx.settings, self.compare_to, data)
                except Exception as exc:  # noqa: BLE001 - a bad --compare-to must not lose the run
                    data["comparison"] = {"error": f"could not compare with {self.compare_to}: {exc}"}
        write_json(self.run_dir / "findings.json", data)


def summarize_dicts(results: list[dict]) -> dict:
    """``probes.summarize`` for results stored as dicts."""
    return summarize([ProbeResult(**r) for r in results])
