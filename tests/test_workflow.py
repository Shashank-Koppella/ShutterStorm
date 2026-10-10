"""End-to-end workflow in simulation mode, including the repair loop and cleanup."""
import json

from shutterstorm.config import ML2, OVERRIDE_FILE
from shutterstorm.approval import Approver
from shutterstorm.workflow import Workflow

YES = Approver("yes", out=lambda text: None)


def test_full_workflow_with_repair(ctx):
    wf = Workflow(ctx, ["ml_pause", "redis_pause", "ml_stop"], repair=True, approver=YES)
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))

    assert data["status"] == "completed", data.get("error")
    assert data["simulated"] is True
    exps = {e["label"]: e for e in data["experiments"]}
    # M-01: hung ML, M-03: hung upload with a ghost upload, M-02: fast failure
    assert exps["ml_pause"]["heatmap"]["J3"] == "hung"
    assert exps["redis_pause"]["heatmap"]["J4"] == "hung"
    assert exps["redis_pause"]["integrity"]["ghost_uploads"] >= 1
    assert exps["ml_stop"]["heatmap"]["J3"] == "fast-fail"
    assert exps["ml_stop"]["scoring"]["score"] > exps["ml_pause"]["scoring"]["score"]

    # Repair: ML failover is verified; Redis has no config-level fix (honest unresolved).
    assert exps["ml_pause"]["repair"]["status"] == "verified"
    assert (run_dir / "patches" / "ml_pause.json").exists()
    assert exps["redis_pause"]["repair"]["status"] == "no_fix_possible"
    assert exps["ml_pause"]["repair"]["attempts"][0]["changed_files"] == [OVERRIDE_FILE]

    # Cleanup always runs and the patch was reverted.
    assert data["cleanup"]["ok"], [c for c in data["cleanup"]["checks"] if not c["ok"]]
    assert ML2 not in ctx.sim.containers
    assert not (ctx.settings.immich_dir / OVERRIDE_FILE).exists()
    assert not ctx.sim.assets, "probe assets must be deleted"

    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "SIMULATED" in html and "Cleanup verification" in html
    assert (run_dir / "SIMULATED.txt").exists()
    assert data["scorecard"]["precision"] > 0
    ranked = data["report"]["ranked_findings"]
    assert [f["severity_value"] for f in ranked] == sorted([f["severity_value"] for f in ranked], reverse=True)


def test_repair_attempts_are_not_repeated(ctx):
    """ml_stop: the failover patch improves less than the bar, so later attempts must differ, then give up."""
    wf = Workflow(ctx, ["ml_stop"], repair=True, approver=YES)
    run_dir = wf.run()
    exp = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))["experiments"][0]
    proposed = [json.dumps(a["proposed"], sort_keys=True) for a in exp["repair"]["attempts"]]
    assert len(proposed) == len(set(proposed))
    assert exp["repair"]["status"] in ("verified", "no_fix_possible", "unresolved")


def test_stop_file_aborts_and_cleans_up(ctx):
    wf = Workflow(ctx, ["ml_pause", "redis_pause"])
    original = wf.run_one

    def run_one_then_stop(spec, label=None):
        exp = original(spec, label)
        ctx.journal.request_stop()
        return exp

    wf.run_one = run_one_then_stop
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert data["status"] == "aborted"
    assert len(data["experiments"]) == 1
    assert ctx.journal.is_empty()
    assert all(c["running"] and not c["paused"] for n, c in ctx.sim.containers.items())


def test_keep_fix_leaves_patch_applied(ctx):
    wf = Workflow(ctx, ["ml_pause"], repair=True, keep_fix=True, approver=YES)
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert data["experiments"][0]["repair"]["kept"] is True
    assert ML2 in ctx.sim.containers
    assert data["cleanup"]["ok"]
