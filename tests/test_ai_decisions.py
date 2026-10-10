"""The AI decides what to break, when, and how safely - within hard bounds it cannot exceed."""
import json

from shutterstorm import planner
from shutterstorm.config import Settings, Thresholds
from shutterstorm.faults import CATALOG
from shutterstorm.llm import LLMClient
from shutterstorm.agentclient import Agents
from shutterstorm.workflow import Workflow

CAPS = Thresholds()  # configured caps: 75 % errors, 8000 ms p95


def test_settings_are_clamped_and_safety_can_only_get_stricter():
    d = planner.decide_settings(CATALOG["traffic_spike"], {"workers": 500, "duration_s": 999, "max_error_rate": 0.95,
                                                           "max_p95_ms": 100, "rm_rf": 1}, CAPS, 60)
    assert d["params"] == {"workers": 48}
    assert d["duration_s"] == 45
    assert d["thresholds"].max_error_rate == 0.75      # looser than the cap -> back to the cap
    assert d["thresholds"].max_p95_ms == 2000          # absurdly strict -> floor
    assert d["thresholds"].blast_radius is True        # never switchable by the AI
    assert any("max_error_rate 0.95" in n for n in d["clamped"]) and any("rm_rf" in n for n in d["clamped"])

    strict = planner.decide_settings(CATALOG["db_cpu_throttle"], {"cpus": 0.15, "max_error_rate": 0.4}, CAPS, 60)
    assert strict["params"] == {"cpus": 0.15} and strict["thresholds"].max_error_rate == 0.4 and not strict["clamped"]
    assert planner.decide_settings(CATALOG["ml_pause"], {"duration_s": "long"}, CAPS, 60)["duration_s"] == 35


def _scripted_llm(tmp_path, decisions):
    """A live LLM whose planner answers are scripted; every other agent gets an invalid answer (-> fallback)."""
    llm = LLMClient(Settings(home=tmp_path, llm_mode="live", mistral_api_key="k"))
    answers = iter(decisions)

    def post(messages):
        prompt = messages[1]["content"]
        if "choose which experiments to run" in prompt:
            return json.dumps({"plan": [{"fault": "ml_pause", "reason": "undeclared server to ml dependency"},
                                        {"fault": "db_pause", "reason": "database hang"}],
                               "strategy": "hangs first"})
        if "deciding the NEXT experiment" in prompt and "was invalid" not in messages[-1]["content"]:
            return json.dumps(next(answers))
        return "{}"
    llm._post = post
    return llm


def test_ai_decides_what_when_and_how_safely_during_the_run(ctx, tmp_path):
    wf = Workflow(ctx, budget=2)
    wf.agents = Agents.local(_scripted_llm(tmp_path, [
        # step 1: deviates from the tentative plan, picks a strength and STRICTER limits
        {"fault": "traffic_spike", "reason": "measure load headroom before the hangs",
         "settings": {"workers": 40, "duration_s": 3, "max_error_rate": 0.5}, "safety_reason": "server is critical"},
        # step 2: an illegal choice -> validator rejects it -> deterministic next step
        {"fault": "docker_rm_everything", "reason": "x", "safety_reason": "y"},
    ]))
    run_dir = wf.run()
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert data["status"] == "completed", data.get("error")
    first, second = data["experiments"]

    assert first["fault"] == "traffic_spike" and first["decision"]["source"] == "live"
    assert first["decision"]["followed_plan"] is False                       # the AI changed its plan
    assert first["decision"]["plan_next"] == data["plan"]["items"][0]["fault"] != "traffic_spike"
    assert first["load"]["workers"] == 40                       # the AI's strength reached the fault
    assert "error rate > 50%" in first["safety"]["thresholds"]  # the AI's stricter limit reached the monitor

    assert second["decision"]["source"] == "fallback"           # illegal fault never ran
    assert second["fault"] in CATALOG and second["fault"] != "traffic_spike"
    assert [d["fault"] for d in data["decisions"]] == ["traffic_spike", second["fault"]]
    assert data["cleanup"]["ok"]
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "AI decisions" in html and "changed plan" in html


def test_deterministic_next_digs_deeper_where_it_hurt(ctx):
    from shutterstorm.topology import discover

    cands = planner.candidates(ctx, discover(ctx), {})
    done = [{"fault": "redis_pause", "severity": 0.09}]
    nxt = planner.deterministic_next(cands, [], done, budget=6)
    assert CATALOG[nxt["fault"]].service == "redis"   # a hurt service gets a priority boost
