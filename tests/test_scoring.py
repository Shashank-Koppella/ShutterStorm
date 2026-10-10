from shutterstorm import scoring
from shutterstorm.topology import JOURNEY_DEPS


def r(journey, ok=True, latency=100.0, timed_out=False):
    return {"journey": journey, "ok": ok, "latency_ms": latency, "timed_out": timed_out}


def test_components_and_score_for_hung_ml():
    baseline = [r(j) for j in JOURNEY_DEPS]
    fault = [r(j) for j in JOURNEY_DEPS if j != "J3"] + [r("J3", ok=False, latency=10000, timed_out=True)]
    evidence = {"fault_results": fault, "service": "ml", "recovery_s": 1.0, "integrity": {"score": 1.0}}
    s = scoring.score_experiment(evidence, baseline, JOURNEY_DEPS, "pause")
    c = s["components"]
    assert c["A"] == round(6 / 7, 3)
    assert c["C"] == 1.0          # journeys independent of ML stayed healthy
    assert c["B"] == 0.0          # the only failure hung
    assert c["R"] == 1.0
    assert 0 < s["score"] < 100
    assert s["severity"] == round((100 - s["score"]) / 100 * 0.6 * 0.5, 3)


def test_fast_failure_scores_better_than_hang():
    baseline = [r("J3")]
    hung = {"fault_results": [r("J3", False, 10000, True)], "service": "ml", "recovery_s": 1, "integrity": {"score": 1}}
    fast = {"fault_results": [r("J3", False, 50, False)], "service": "ml", "recovery_s": 1, "integrity": {"score": 1}}
    deps = {"J3": ["ml"]}
    assert scoring.score_experiment(fast, baseline, deps, "stop")["score"] > \
        scoring.score_experiment(hung, baseline, deps, "pause")["score"]


def test_recovery_component_is_linear():
    assert scoring.recovery_component(10) == 1.0
    assert scoring.recovery_component(30) == 1.0
    assert scoring.recovery_component(165) == 0.5
    assert scoring.recovery_component(300) == 0.0
    assert scoring.recovery_component(None) == 0.0


def test_latency_component_and_classification():
    baseline = [r("J2", latency=100)]
    slow = [r("J2", latency=800)]
    assert scoring.latency_component(baseline, slow) == 0.125
    assert scoring.classify_cell(slow, baseline, "J2") == "slow"  # > max(2x, +500 ms)
    assert scoring.classify_cell([r("J2", latency=400)], baseline, "J2") == "ok"
    assert scoring.classify_cell([r("J2", False, 50)], baseline, "J2") == "fast-fail"
    assert scoring.classify_cell([r("J2", False, 9000, True)], baseline, "J2") == "hung"
    assert scoring.classify_cell([], baseline, "J2") == "n/a"


def test_overall_score_is_criticality_weighted():
    exps = [{"scoring": {"criticality": 1.0, "score": 50}}, {"scoring": {"criticality": 0.6, "score": 90}}]
    assert scoring.overall_score(exps) == round((50 + 0.6 * 90) / 1.6, 1)
