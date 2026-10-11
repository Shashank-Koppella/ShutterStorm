"""Dashboard data contracts (webapi) and HTTP hardening (dashboard), on a simulated run."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from shutterstorm import webapi
from shutterstorm.config import Settings
from shutterstorm.dashboard import make_server
from shutterstorm.workflow import Workflow


@pytest.fixture
def sim_run(ctx):
    run_dir = Workflow(ctx, ["db_pause", "spike_redis_hung"]).run()
    return ctx, run_dir


def test_run_ids_are_validated_and_modes_kept_apart(sim_run, tmp_path):
    ctx, run_dir = sim_run
    s = ctx.settings
    assert webapi.run_dir(s, run_dir.name) == run_dir.resolve()
    for bad in ("../secrets", "..", "20261011-000000-run/../../x", "not-a-run", "", "20261011-000000-nope-SIM"):
        assert webapi.run_dir(s, bad) is None
    real = Settings(home=s.home, simulate=False)
    assert webapi.run_dir(real, run_dir.name) is None          # a SIM run is invisible to the real dashboard
    assert webapi.list_runs(real) == []
    assert [r["id"] for r in webapi.list_runs(s)] == [run_dir.name]


def test_run_detail_has_no_raw_probe_lists_and_is_severity_ordered(sim_run):
    ctx, run_dir = sim_run
    d = webapi.run_detail(ctx.settings, run_dir.name)
    assert d["simulated"] is True and d["status"] == "completed"
    sev = [e["scoring"]["severity"] for e in d["experiments"]]
    assert sev == sorted(sev, reverse=True)
    for e in d["experiments"]:
        assert not {"fault_results", "recovery_results", "log_tails"} & set(e)
        assert "details" not in e["integrity"] and e["has_evidence"]
    assert d["cleanup"]["ok"] and d["has_events"] and d["has_topology"]


def test_timeline_topology_and_events(sim_run):
    ctx, run_dir = sim_run
    t = webapi.timeline(ctx.settings, run_dir.name, "db_pause")
    assert t["t0_source"] == "injection" and t["undo_exact"] is True
    assert any(p["outcome"] == "hung" for p in t["series"]["J1"])
    assert webapi.timeline(ctx.settings, run_dir.name, "../evidence") is None
    topo = webapi.topology(ctx.settings, run_dir.name)
    assert topo["available"] and any("ml" in u for u in topo["undeclared"])
    assert {e["source"] for e in topo["edges"]} <= {"compose", "config", "empirical"}
    assert "database" in topo["impacts"]
    ev = webapi.events(ctx.settings, run_dir.name, 0)
    types = [e["type"] for e in ev["events"]]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert {"decision", "injected", "probe_round", "safety_check", "rollback", "undone", "experiment_done", "cleanup"} <= set(types)
    later = webapi.events(ctx.settings, run_dir.name, ev["last"] - 3)
    assert [e["seq"] for e in later["events"]] == [ev["last"] - 2, ev["last"] - 1, ev["last"]]


def test_catalog_lists_caps_floors_and_knobs(ctx):
    cat = webapi.catalog(ctx.settings)
    assert cat["caps"]["max_error_rate"] == 0.75 and cat["floors"]["max_error_rate"] == 0.25
    knobs = {f["id"]: f["knob"] for f in cat["faults"]}
    assert knobs["traffic_spike"]["name"] == "workers" and knobs["ml_pause"] is None


@pytest.fixture
def server(tmp_path):
    srv, sctx = make_server(Settings(simulate=True, home=tmp_path, llm_mode="off"), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    sctx.close()


def _req(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http_hardening(server):
    code, headers, body = _req(server + "/")
    assert code == 200 and b"/static/js/app.js" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    assert _req(server + "/static/app.css")[0] == 200
    for bad in ("/static/../dashboard.py", "/static/%2e%2e/dashboard.py", "/static/js/../../config.py", "/static/index.html"):
        assert _req(server + bad)[0] == 404, bad
    assert _req(server + "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert _req(server + "/api/runs/..%2f..%2fetc")[0] == 404
    status = json.loads(_req(server + "/api/status")[2])
    assert status["mode"] == "simulated" and len(status["containers"]) == 4
    # POST: a cross-site form cannot set the custom header -> refused
    body = json.dumps({"fault": "ml_pause"}).encode()
    assert _req(server + "/api/run", "POST", {"Content-Type": "application/json"}, body)[0] == 403
    assert _req(server + "/api/run", "POST", {"Content-Type": "text/plain", "X-ShutterStorm": "1"}, body)[0] == 403
    ok = {"Content-Type": "application/json", "X-ShutterStorm": "1"}
    assert _req(server + "/api/run", "POST", ok, json.dumps({"fault": "rm_rf"}).encode())[0] == 400
    assert _req(server + "/api/plan", "POST", ok, json.dumps({"budget": "lots"}).encode())[0] == 400
    assert json.loads(_req(server + "/api/stop", "POST", ok, b"{}")[2]) == {"stop": "requested"}
