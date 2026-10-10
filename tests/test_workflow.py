"""End-to-end workflow in simulation mode, including fix suggestions and cleanup."""
import json

from shutterstorm.config import ML2, OVERRIDE_FILE
from shutterstorm.sim import default_system_config
from shutterstorm.workflow import Workflow


def run(ctx, faults, **kwargs):
    run_dir = Workflow(ctx, faults, **kwargs).run()
    return run_dir, json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))


def test_full_workflow_with_suggestions(ctx):
    run_dir, data = run(ctx, ["ml_pause", "redis_pause", "ml_stop"])

    assert data["status"] == "completed", data.get("error")
    assert data["simulated"] is True
    exps = {e["label"]: e for e in data["experiments"]}
    # M-01: hung ML, M-03: hung upload with a ghost upload, M-02: fast failure
    assert exps["ml_pause"]["heatmap"]["J3"] == "hung"
    assert exps["redis_pause"]["heatmap"]["J4"] == "hung"
    assert exps["redis_pause"]["integrity"]["ghost_uploads"] >= 1
    assert exps["ml_stop"]["heatmap"]["J3"] == "fast-fail"
    assert exps["ml_stop"]["scoring"]["score"] > exps["ml_pause"]["scoring"]["score"]

    # Every affected fault gets a suggestion; none is applied.
    ml = exps["ml_pause"]["suggestion"]
    assert ml["kind"] == "config" and ml["valid"] and "machineLearning.urls" in ml["diff"]
    assert ml["status"].startswith("suggestion only")
    assert exps["redis_pause"]["suggestion"]["kind"] == "code"

    assert data["cleanup"]["ok"], [c for c in data["cleanup"]["checks"] if not c["ok"]]
    assert not ctx.sim.assets, "probe assets must be deleted"
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "SIMULATED" in html and "Suggested fix" in html and "not applied" in html
    ranked = data["report"]["ranked_findings"]
    assert [f["severity_value"] for f in ranked] == sorted([f["severity_value"] for f in ranked], reverse=True)


def test_suggestions_never_change_immich(ctx):
    """The tool must not touch Immich's config, compose files or containers beyond the injected faults."""
    files_before = sorted(p.name for p in ctx.settings.immich_dir.iterdir())
    run(ctx, ["ml_pause", "ml_stop"])
    assert ctx.sim.system_config == default_system_config()
    assert sorted(p.name for p in ctx.settings.immich_dir.iterdir()) == files_before
    assert not (ctx.settings.immich_dir / OVERRIDE_FILE).exists()
    assert ML2 not in ctx.sim.containers
    # No docker compose "up" was ever run (that is how a fix would be applied).
    assert not any(c[:1] == ["compose"] and "up" in c for c in ctx.sim.docker.calls)


def test_no_suggestions_flag(ctx):
    _, data = run(ctx, ["ml_pause"], suggest=False)
    assert "suggestion" not in data["experiments"][0]


def test_stop_file_aborts_and_cleans_up(ctx):
    wf = Workflow(ctx, ["ml_pause", "redis_pause"])
    original = wf.run_one

    def run_one_then_stop(spec, decision):
        exp = original(spec, decision)
        ctx.journal.request_stop()
        return exp

    wf.run_one = run_one_then_stop
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert data["status"] == "aborted"
    assert len(data["experiments"]) == 1
    assert ctx.journal.is_empty()
    assert all(c["running"] and not c["paused"] for n, c in ctx.sim.containers.items())
