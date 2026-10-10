"""Container mode: agents over HTTP, controller-side re-checks, heartbeat guard, scope of our own containers."""
import json
import os
import threading
import time

import pytest

from shutterstorm import planner
from shutterstorm.agentclient import Agents
from shutterstorm.agents_service import make_server
from shutterstorm.config import Settings, Thresholds
from shutterstorm.context import build_context
from shutterstorm.dockerctl import ScopeError
from shutterstorm.faults import FaultError, _restore_cpus
from shutterstorm.guard import entry_is_orphaned
from shutterstorm.journal import heartbeat_path
from shutterstorm.topology import discover
from shutterstorm.workflow import Workflow


@pytest.fixture
def agents_url(tmp_path):
    server = make_server(Settings(home=tmp_path / "agents", llm_mode="off"), "127.0.0.1", 0, log=lambda m: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_full_workflow_with_agents_in_a_separate_service(tmp_path, agents_url):
    ctx = build_context(Settings(simulate=True, home=tmp_path, llm_mode="off", agents_url=agents_url))
    try:
        run_dir = Workflow(ctx, budget=3).run()
        data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
        assert data["status"] == "completed", data.get("error")
        assert len(data["experiments"]) == 3 and data["cleanup"]["ok"]
        assert data["llm_stats"]["agents"] == f"agents container {agents_url}"
        assert data["llm_stats"]["fallback"] > 0          # every agent answered (templates, LLM off)
        assert all(e["analysis"]["summary"] for e in data["experiments"])
    finally:
        ctx.close()


def test_unreachable_agents_service_falls_back_to_templates(ctx):
    agents = Agents(ctx.settings.with_overrides(agents_url="http://127.0.0.1:9"), log=lambda m: None)
    p = agents.plan(discover(ctx), planner.candidates(ctx, discover(ctx), {}), 3)
    assert p["source"] == "fallback" and len(p["items"]) == 3


def test_controller_rechecks_what_the_agent_returns(ctx):
    cands = planner.candidates(ctx, discover(ctx), {})
    the_plan = {"items": [], "candidates": cands}
    agents = Agents(ctx.settings.with_overrides(agents_url="http://agents.invalid"), log=lambda m: None)
    # A compromised or buggy agent answers with a forbidden fault and loose limits.
    agents._call = lambda name, **kw: {"fault": "rm_rf_root", "reason": "x", "safety_reason": "y", "source": "live",
                                       "settings": {"max_error_rate": 1.0, "workers": 10_000}}
    d = agents.decide_next(the_plan, [], Thresholds(), 3, 60, 35)
    assert d["fault"] in {c["fault"] for c in cands} and d["source"] == "fallback"
    agents._call = lambda name, **kw: {"fault": "traffic_spike", "reason": "x", "safety_reason": "y", "source": "live",
                                       "settings": {"max_error_rate": 1.0, "workers": 10_000, "duration_s": 600}}
    d = agents.decide_next(the_plan, [], Thresholds(), 3, 60, 35)
    assert d["thresholds"].max_error_rate == 0.75 and d["params"]["workers"] == 48 and d["duration_s"] == 45


def test_guard_uses_heartbeats_for_owners_in_other_containers(tmp_path):
    entry = {"pid": 1, "host": "controller-container", "owner": "controller-container-1", "deadline": time.time() + 60}
    assert "heartbeats" in entry_is_orphaned(entry, time.time(), state_dir=tmp_path)       # never beat
    path = heartbeat_path(tmp_path, "controller-container-1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"t": time.time()}), encoding="utf-8")
    assert entry_is_orphaned(entry, time.time(), state_dir=tmp_path) is None              # alive
    path.write_text(json.dumps({"t": time.time() - 60}), encoding="utf-8")
    assert "stopped sending heartbeats" in entry_is_orphaned(entry, time.time(), state_dir=tmp_path)


def test_our_own_containers_are_never_fault_targets(ctx):
    for name in ("shutterstorm-controller", "shutterstorm-agents", "shutterstorm-guard"):
        with pytest.raises(ScopeError):
            ctx.docker.pause(name)


def test_no_compose_recreate_from_inside_a_container(ctx, monkeypatch):
    monkeypatch.setenv("SHUTTERSTORM_IN_CONTAINER", "1")
    with pytest.raises(FaultError):
        _restore_cpus(ctx, "immich_machine_learning", 0)
    assert not any(c[:1] == ["compose"] for c in ctx.sim.docker.calls)
    assert os.environ["SHUTTERSTORM_IN_CONTAINER"] == "1"
