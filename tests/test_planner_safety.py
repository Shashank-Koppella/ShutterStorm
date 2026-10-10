"""AI planner, threshold auto-rollback, combined failures, traffic spike and before/after comparison."""
import json

from shutterstorm import planner
from shutterstorm.config import ML, REDIS, Settings, Thresholds
from shutterstorm.faults import CATALOG
from shutterstorm.llm import LLMClient
from shutterstorm.probes import ProbeResult
from shutterstorm.safety import SafetyMonitor, protected_journeys
from shutterstorm.topology import discover
from shutterstorm.workflow import Workflow


def res(journey, ok=True, latency=100.0, timed_out=False):
    return ProbeResult(journey, ok, 200 if ok else None, latency, timed_out)


# ---------------------------------------------------------------------- planner
def test_deterministic_plan_is_diverse_and_covers_the_map(ctx):
    llm = LLMClient(ctx.settings)
    p = planner.plan(ctx, llm, discover(ctx), budget=6)
    assert p["source"] == "fallback"
    faults = [i["fault"] for i in p["items"]]
    assert len(faults) == 6 and len(set(faults)) == 6
    specs = [CATALOG[f] for f in faults]
    assert all(not s.optional for s in specs)                             # only planner-eligible faults
    assert len({i["failure_class"] for i in p["items"]}) >= planner.MIN_CLASSES
    assert {s.service for s in specs} == {"server", "ml", "redis", "database"}  # every service covered
    assert sum(bool(s.parts) for s in specs) <= planner.MAX_COMBOS
    assert all(i["reason"] for i in p["items"])
    assert [i["priority"] for i in p["items"]] == sorted([i["priority"] for i in p["items"]], reverse=True)
    assert {s["fault"] for s in p["skipped"]} == set(CATALOG) - set(faults)


def test_planner_uses_valid_llm_plan_and_rejects_invalid_ones(ctx, tmp_path):
    topo = discover(ctx)
    cands = planner.candidates(ctx, topo, {})
    good = {"plan": [{"fault": f, "reason": "covers the undeclared server to ml dependency"}
                     for f in ["ml_pause", "db_cpu_throttle", "server_restart"]], "strategy": "riskiest first"}
    assert planner.check_plan(good, cands, 3) == []
    bad_fault = {"plan": [{"fault": "rm_rf_everything", "reason": "x"}] + good["plan"][1:]}
    assert any("not an available catalog fault" in e for e in planner.check_plan(bad_fault, cands, 3))
    optional = {"plan": [{"fault": "db_stop", "reason": "x"}] + good["plan"][1:]}
    assert planner.check_plan(optional, cands, 3)
    one_class = {"plan": [{"fault": f, "reason": "x"} for f in ["ml_pause", "redis_pause", "db_pause"]]}
    assert any("failure classes" in e for e in planner.check_plan(one_class, cands, 3))

    llm = LLMClient(Settings(home=tmp_path, llm_mode="live", mistral_api_key="k"))
    llm._post = lambda messages: json.dumps(good)
    p = planner.plan(ctx, llm, topo, budget=3)
    assert p["source"] == "live" and [i["fault"] for i in p["items"]] == ["ml_pause", "db_cpu_throttle", "server_restart"]

    llm2 = LLMClient(Settings(home=tmp_path / "b", llm_mode="live", mistral_api_key="k"))
    llm2._post = lambda messages: json.dumps(bad_fault)
    assert planner.plan(ctx, llm2, topo, budget=3)["source"] == "fallback"


# ---------------------------------------------------------------------- safety monitor
def test_protected_journeys_define_the_blast_radius():
    assert protected_journeys(CATALOG["ml_pause"]) == ["J0", "J1", "J2", "J4", "J5", "J6"]
    assert protected_journeys(CATALOG["db_pause"]) == ["J0"]
    assert protected_journeys(CATALOG["server_restart"]) == []
    assert protected_journeys(CATALOG["spike_redis_hung"]) == ["J0", "J1", "J2", "J3", "J6"]


def test_monitor_needs_consecutive_breaches():
    m = SafetyMonitor(Thresholds(max_error_rate=0.5, rounds=2), CATALOG["db_pause"])
    bad = [res("J0")] + [res(j, ok=False, latency=10000, timed_out=True) for j in ("J1", "J2", "J3", "J4")]
    assert m.check(bad) is None                     # 1st breached round
    assert m.check([res("J0"), res("J1")]) is None  # healthy round resets the streak
    assert m.check(bad) is None
    assert "error rate 80%" in m.check(bad)


def test_restart_breaches_are_recorded_but_not_rolled_back():
    m = SafetyMonitor(Thresholds(rounds=1), CATALOG["server_restart"])
    assert m.check([res(j, ok=False) for j in ("J0", "J1", "J2")]) is None
    assert m.history[0]["breached"]


def test_monitor_latency_and_blast_radius():
    m = SafetyMonitor(Thresholds(max_p95_ms=1000, rounds=1), CATALOG["ml_cpu_throttle"])
    assert "p95 latency" in m.check([res("J3", latency=5000)])
    m = SafetyMonitor(Thresholds(rounds=1), CATALOG["ml_pause"])
    assert m.check([res("J3", ok=False, timed_out=True), res("J0")]) is None   # J3 is the expected victim
    assert "blast radius" in m.check([res("J2", ok=False), res("J0")])          # J2 does not need ML


def test_workflow_rolls_back_on_threshold_breach(ctx):
    wf = Workflow(ctx, ["ml_pause"], thresholds=Thresholds(max_error_rate=0.05, rounds=1))
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    exp = data["experiments"][0]
    assert data["status"] == "completed"
    assert exp["auto_stopped"] and "error rate" in exp["auto_stopped"]["reason"]
    assert exp["auto_stopped"]["round"] == 1
    assert not ctx.sim.containers[ML]["paused"] and ctx.journal.is_empty()
    assert "AUTO-STOPPED" in (run_dir / "report.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- new faults
def test_combo_and_traffic_spike_in_workflow(ctx):
    wf = Workflow(ctx, ["spike_redis_hung", "db_cpu_throttle"])
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert data["status"] == "completed", data.get("error")
    exps = {e["fault"]: e for e in data["experiments"]}
    combo = exps["spike_redis_hung"]
    assert combo["parts"] == ["traffic_spike", "redis_pause"]
    assert combo["load"]["sent"] > 0 and combo["load"]["workers"] == 24
    assert combo["heatmap"]["J4"] == "hung"
    assert exps["db_cpu_throttle"]["heatmap"]["J2"] in ("ok", "slow")
    assert not ctx.sim.containers[REDIS]["paused"] and ctx.sim.containers["immich_postgres"]["quota"] == 0
    assert data["cleanup"]["ok"]
    assert data["plan"]["source"] == "operator"


def test_planned_workflow_and_comparison(ctx):
    first = Workflow(ctx, budget=3).run()
    d1 = json.loads((first / "findings.json").read_text(encoding="utf-8"))
    assert d1["plan"]["source"] == "fallback" and len(d1["experiments"]) == 3
    assert [e["fault"] for e in d1["experiments"]] == [i["fault"] for i in d1["plan"]["items"]]
    faults = [e["fault"] for e in d1["experiments"]]
    second = Workflow(ctx, faults, compare_to=first.name).run()
    d2 = json.loads((second / "findings.json").read_text(encoding="utf-8"))
    comp = d2["comparison"]
    assert comp["before_run"] == first.name and len(comp["rows"]) == 3
    assert all(r["verdict"] in ("improved", "regressed", "no clear change") for r in comp["rows"])
    html = (second / "report.html").read_text(encoding="utf-8")
    assert "Before / after" in html and "Experiment plan" in (first / "report.html").read_text(encoding="utf-8")
