"""LLM modes, probes helpers, redaction, topology."""
import json
import zlib

from shutterstorm.agents import check_analysis, fallback_repair
from shutterstorm.config import Settings, parse_env_file, redact
from shutterstorm.llm import LLMClient
from shutterstorm.probes import make_png, percentile
from shutterstorm.topology import discover, predict_impact, prediction_scorecard, score_prediction
from shutterstorm.faults import CATALOG


def test_png_is_valid_and_unique():
    a, b = make_png(), make_png()
    assert a.startswith(b"\x89PNG\r\n\x1a\n") and a != b
    idat = a[a.index(b"IDAT") + 4:a.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == 64 * (64 * 3 + 1)


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([1, 2, 3, 4, 100], 50) == 3
    assert percentile([1, 2, 3, 4, 100], 95) == 100


def test_redact_hides_secrets_and_tokens():
    text = 'Authorization: Bearer abc.def-123 {"accessToken": "tok123"} pw=hunter2'
    out = redact(text, ["hunter2"])
    assert "abc.def-123" not in out and "tok123" not in out and "hunter2" not in out


def test_env_file_parsing(tmp_path):
    p = tmp_path / "shutterstorm.env"
    p.write_text("A=1\n# comment\nLLM_MODE=live            # live | replay | off\nB='x y'\n", encoding="utf-8")
    assert parse_env_file(p) == {"A": "1", "LLM_MODE": "live", "B": "x y"}


def _llm(tmp_path, mode, key=""):
    return LLMClient(Settings(home=tmp_path, llm_mode=mode, mistral_api_key=key))


def test_llm_off_and_replay_and_live_validation(tmp_path):
    msgs = [{"role": "user", "content": "hi"}]
    ok = lambda d: [] if "summary" in d else ["missing summary"]  # noqa: E731

    assert _llm(tmp_path, "off").complete_json(msgs, ok) == (None, "fallback")
    assert _llm(tmp_path, "replay").complete_json(msgs, ok) == (None, "fallback")   # cache miss

    live = _llm(tmp_path, "live", key="k")
    answers = iter(["not json", json.dumps({"summary": "fine"})])
    live._post = lambda messages: next(answers)  # first answer invalid -> one retry
    assert live.complete_json(msgs, ok) == ({"summary": "fine"}, "live")

    assert _llm(tmp_path, "replay").complete_json(msgs, ok) == ({"summary": "fine"}, "cache")

    bad = _llm(tmp_path, "live", key="k")
    bad._post = lambda messages: "{}"
    assert bad.complete_json([{"role": "user", "content": "other"}], ok) == (None, "fallback")


def test_schema_checks():
    assert check_analysis({}) != []
    assert check_analysis({"summary": "", "affected_journeys": [], "root_cause_hypothesis": "",
                           "user_visible_symptom": "", "severity_rationale": ""}) == []


def test_fallback_repair_gives_up_for_redis():
    exp = {"fault": "redis_pause"}
    assert fallback_repair(exp, [])["no_fix_possible"] is True
    first = fallback_repair({"fault": "ml_pause"}, [])
    second = fallback_repair({"fault": "ml_pause"}, [{"proposed": first["patch"]}])
    assert first["patch"] != second["patch"]


def test_discover_finds_undeclared_ml_dependency(ctx):
    topo = discover(ctx)
    assert any(e["to"] == "ml" and e["source"] == "config" for e in topo["edges"])
    assert any("ml" in u for u in topo["undeclared_dependencies"])
    assert predict_impact(CATALOG["ml_pause"], topo)["predicted_impacted"] == ["J3"]


def test_prediction_scorecard():
    exps = [{"fault": "a", "prediction_check": score_prediction(["J3"], ["J3", "J6"])}]
    sc = prediction_scorecard(exps)
    assert sc["precision"] == 1.0 and sc["recall"] == 0.5 and sc["mismatches"][0]["unexpected_impact"] == ["J6"]


def test_grounding_rejects_invented_numbers_and_other_faults():
    from shutterstorm.agents import grounding_errors

    payload = {"score": 85.12, "recovery_s": 0.6, "sample_errors": ["no response within 10s"], "window_s": 35.0}
    ok = {"summary": "J3 hung for 10s; score 85.1, recovered in 0.6 s (600 ms) after a 35 s window."}
    assert grounding_errors(ok, ["summary"], payload, "ml_pause") == []
    invented = {"summary": "Latency rose to 4321 ms and 73.5% of calls failed."}
    assert any("4321" in e and "73.5" in e for e in grounding_errors(invented, ["summary"], payload))
    blamed = {"summary": "J3 later failed because of ml_stop."}
    assert any("ml_stop" in e for e in grounding_errors(blamed, ["summary"], payload, "ml_pause"))


def test_analyst_falls_back_when_llm_hallucinates(tmp_path):
    from shutterstorm import agents

    llm = _llm(tmp_path, "live", key="k")
    invented = json.dumps({"summary": "99.97% of 4321 requests failed", "affected_journeys": ["J3"],
                           "root_cause_hypothesis": "x", "user_visible_symptom": "y", "severity_rationale": "z"})
    llm._post = lambda messages: invented
    payload = {"score": 85.1}
    data, source = llm.complete_json([{"role": "user", "content": "q"}],
                                     agents._grounded(agents.check_analysis, agents.ANALYSIS_TEXT, payload, "ml_pause"))
    assert (data, source) == (None, "fallback")
