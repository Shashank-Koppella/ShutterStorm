"""Composed fault catalog: fault types x discovered targets, safety policy, aliases, AI composition."""
import json

from shutterstorm import planner
from shutterstorm.agentclient import Agents
from shutterstorm.config import ML, POSTGRES, REDIS, SERVER, Settings
from shutterstorm.faults import (ALIASES, CATALOG, FAULT_TYPES, MANUAL_ONLY, TARGETS, apply_fault, apply_faults,
                                 get_fault, lookup, undo_faults)
from shutterstorm.llm import LLMClient
from shutterstorm.safety import panic
from shutterstorm.topology import discover
from shutterstorm.workflow import Workflow


def test_every_type_target_pair_is_composed_and_policy_applied():
    for service in TARGETS:
        for kind in FAULT_TYPES:
            specs = [s for s in CATALOG.values() if s.kind == kind and s.service == service and not s.parts]
            assert len(specs) == 1, (service, kind)
            assert specs[0].optional == ((service, kind) in MANUAL_ONLY)
    combos = [s for s in CATALOG.values() if s.parts]
    assert combos and all(s.parts[0] == "traffic_spike" and s.service != "server" and not CATALOG[s.parts[1]].optional
                          for s in combos)
    assert CATALOG["db_stop"].optional and CATALOG["db_restart"].optional    # Postgres is never stopped automatically
    assert not CATALOG["redis_cpu_throttle"].optional                      # a pair nobody wrote down by hand
    for old, new in ALIASES.items():
        assert get_fault(old) is CATALOG[new] and lookup(old) is CATALOG[new]
    assert lookup("rm_rf_everything") is None


def test_candidates_only_target_services_in_the_map(ctx):
    topo = discover(ctx)
    cands = {c["fault"]: c for c in planner.candidates(ctx, topo, {})}
    assert cands["redis_cpu_throttle"]["available"] and cands["redis_cpu_throttle"]["target"] == "redis"
    assert cands["spike_db_latency"]["with_traffic_spike"] is True
    assert not cands["db_stop"]["available"] and "safety policy" in cands["db_stop"]["unavailable_reason"]
    no_redis = {**topo, "edges": [e for e in topo["edges"] if e["to"] != "redis"],
                "nodes": {k: v for k, v in topo["nodes"].items() if k != "redis"}}
    cands2 = {c["fault"]: c for c in planner.candidates(ctx, no_redis, {})}
    assert not cands2["redis_pause"]["available"]
    assert "not in the discovered dependency map" in cands2["redis_pause"]["unavailable_reason"]
    assert not cands2["spike_redis_pause"]["available"]


def test_planner_prompt_stays_within_its_cap(ctx, tmp_path):
    seen = {}
    llm = LLMClient(Settings(home=tmp_path, llm_mode="live", mistral_api_key="k"))

    def post(messages):
        seen["len"] = len(messages[1]["content"])
        return "{}"
    llm._post = post
    planner.plan(ctx, llm, discover(ctx), 6)
    assert 0 < seen["len"] < 48000


def test_network_faults_on_any_target_apply_undo_and_panic(ctx, checkpoint):
    actives = apply_faults(ctx, CATALOG["db_latency"])
    assert ctx.sim.netem_targets == {POSTGRES: "delay"}
    ok, detail = undo_faults(ctx, actives)
    assert ok, detail
    assert not ctx.sim.netem and not ctx.sim.helpers

    active = apply_fault(ctx, CATALOG["redis_packet_loss"])
    active._timer.cancel()
    assert ctx.sim.netem_targets == {REDIS: "loss"}
    assert panic(ctx, reason="test")["journal_left"] == 0
    assert not ctx.sim.netem and not ctx.sim.helpers


def test_ai_composes_a_pair_nobody_wrote_down(ctx, tmp_path):
    llm = LLMClient(Settings(home=tmp_path, llm_mode="live", mistral_api_key="k"))
    answers = iter([{"fault": "redis_cpu_throttle", "reason": "the job queue under CPU pressure was never tested",
                     "settings": {"cpus": 0.2, "duration_s": 3}, "safety_reason": "queue is critical"}])

    def post(messages):
        prompt = messages[1]["content"]
        if "deciding the NEXT experiment" in prompt and "was invalid" not in messages[-1]["content"]:
            return json.dumps(next(answers, {}))
        return "{}"
    llm._post = post
    wf = Workflow(ctx, budget=1)
    wf.agents = Agents.local(llm)
    data = json.loads((wf.run() / "findings.json").read_text(encoding="utf-8"))
    exp = data["experiments"][0]
    assert data["status"] == "completed", data.get("error")
    assert exp["fault"] == "redis_cpu_throttle" and exp["decision"]["source"] == "live"
    assert exp["params"] == {"cpus": 0.2}
    assert ctx.sim.containers[REDIS]["quota"] == 0 and data["cleanup"]["ok"]


def test_freezing_the_entry_point_is_rolled_back(ctx):
    data = json.loads((Workflow(ctx, ["server_pause"]).run() / "findings.json").read_text(encoding="utf-8"))
    exp = data["experiments"][0]
    assert data["status"] == "completed", data.get("error")
    assert exp["heatmap"]["J0"] == "hung" and exp["auto_stopped"]          # everything hangs -> 100% errors
    assert not ctx.sim.containers[SERVER]["paused"] and not ctx.sim.containers[ML]["paused"]
