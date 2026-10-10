"""The four AI agents behind one interface: in-process, or in the isolated agents container.

``Agents(settings)`` runs the agents in this process unless ``AGENTS_URL`` is set; then every
call goes over HTTP to ``python -m shutterstorm.agents_service`` (the ``agents`` container,
which has the Mistral key but no Docker socket and no route to Immich).

Wherever the agents run, the CONTROLLER re-checks what comes back, because only the
controller can act on it:

* plan          -> catalog faults only, selection rules (``planner.check_plan``)
* next decision -> fault must be allowed next; strength, duration and safety limits are
                   clamped again here (``planner.decide_settings``), stricter-only
* prediction    -> only real journey ids
* report        -> order and severities forced from the scoring formula

If the agents service is unreachable, the controller falls back to the deterministic
templates (``LLM_MODE=off`` behaviour) instead of failing the run.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import requests

from . import agents as agent_fns
from . import planner
from .config import Settings, Thresholds
from .faults import CATALOG
from .llm import LLMClient
from .probes import JOURNEYS

TIMEOUT_S = 180  # one agent call may include an LLM retry with backoff


# ---------------------------------------------------------------- the functions both sides run
def _plan(llm, topology, cands, budget):
    return planner.plan_from(llm, topology, cands, budget)


def _decide_next(llm, the_plan, done_exps, caps, budget, max_fault_s, default_window):
    caps = caps if isinstance(caps, Thresholds) else Thresholds(**caps)
    d = planner.decide_next_from(llm, the_plan, done_exps, caps, budget, max_fault_s, default_window)
    return planner.decision_record(d) if d else None


def _predict(llm, fault, topology, deterministic):
    return agent_fns.predict(llm, CATALOG[fault], topology, deterministic)


def _analyze(llm, exp, topology):
    return agent_fns.analyze(llm, exp, topology)


def _suggest_fix(llm, exp, current):
    return agent_fns.suggest_fix(llm, exp, current)


def _write_report(llm, experiments, overall, scorecard, manual):
    return agent_fns.write_report(llm, experiments, overall, scorecard, manual)


FUNCTIONS = {"plan": _plan, "decide_next": _decide_next, "predict": _predict, "analyze": _analyze,
             "suggest_fix": _suggest_fix, "write_report": _write_report}


def run_local(llm: LLMClient, name: str, args: dict):
    return FUNCTIONS[name](llm, **args)


# ---------------------------------------------------------------- client
class Agents:
    """Planner / Analyst / Fix Advisor / Reporter, local or remote, re-checked by the controller."""

    def __init__(self, settings: Settings, log=None, run_id: str = "") -> None:
        self.settings = settings
        self.log = log or (lambda msg: None)
        self.url = (settings.agents_url or "").rstrip("/")
        self.run_id = run_id
        self.llm = None if self.url else LLMClient(settings, self.log)
        self._offline: LLMClient | None = None
        self._remote_stats = {"live": 0, "cache": 0, "fallback": 0, "api_calls": 0}

    @classmethod
    def local(cls, llm: LLMClient) -> "Agents":
        a = cls.__new__(cls)
        a.settings, a.log, a.url, a.run_id, a.llm = llm.settings, llm.log, "", "", llm
        a._offline, a._remote_stats = None, {"live": 0, "cache": 0, "fallback": 0, "api_calls": 0}
        return a

    @property
    def where(self) -> str:
        return f"agents container {self.url}" if self.url else "in-process"

    @property
    def stats(self) -> dict:
        if self.llm is not None:
            return {**self.llm.stats, "api_calls": self.llm.calls}
        out = dict(self._remote_stats)
        if self._offline is not None:
            out["fallback"] += self._offline.stats["fallback"]
        return out

    def _call(self, name: str, **args):
        if not self.url:
            return run_local(self.llm, name, args)
        try:
            resp = requests.post(f"{self.url}/agents/{name}", timeout=TIMEOUT_S,
                                 data=json.dumps({"run_id": self.run_id, "args": args}, default=_json_default),
                                 headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            body = resp.json()
            for k, v in (body.get("stats") or {}).items():
                self._remote_stats[k] = self._remote_stats.get(k, 0) + v
            return body["result"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            self.log(f"agents service unreachable or failed ({exc.__class__.__name__}); using deterministic templates")
            if self._offline is None:
                self._offline = LLMClient(self.settings.with_overrides(llm_mode="off"), self.log)
            return run_local(self._offline, name, args)

    # ------------------------------------------------------------ agents + controller-side checks
    def plan(self, topology: dict, cands: list[dict], budget: int) -> dict:
        result = self._call("plan", topology=topology, cands=cands, budget=budget)
        errors = planner.check_plan({"plan": [{"fault": i["fault"], "reason": i.get("reason", "")}
                                              for i in result.get("items", [])]}, cands, budget)
        if errors:
            self.log(f"controller rejected the plan ({'; '.join(errors)[:200]}); using the deterministic plan")
            offline = LLMClient(self.settings.with_overrides(llm_mode="off"), self.log)
            result = planner.plan_from(offline, topology, cands, budget)
            result["source"] = "fallback"
        result["candidates"] = cands  # the controller's own copy, never the agent's
        return result

    def decide_next(self, the_plan: dict, done_exps: list[dict], caps: Thresholds, budget: int,
                    max_fault_s: float, default_window: float) -> dict | None:
        caps_d = asdict(caps)
        d = self._call("decide_next", the_plan=the_plan, done_exps=done_exps, caps=caps_d, budget=budget,
                       max_fault_s=max_fault_s, default_window=default_window)
        cands = the_plan["candidates"]
        picked = [c for e in done_exps for c in cands if c["fault"] == e["fault"]]
        min_classes = planner.min_classes_for(cands, budget)
        allowed = {c["fault"] for c in cands if planner.allowed_next(c, picked, budget, min_classes)}
        if d is None:
            return None
        if d.get("fault") not in allowed:  # never trust the agent's choice blindly
            self.log(f"controller rejected next fault {d.get('fault')!r}; using the deterministic choice")
            nxt = planner.deterministic_next(cands, [i for i in the_plan["items"]
                                                     if i["fault"] not in {e["fault"] for e in done_exps}],
                                             [planner.observation(e) for e in done_exps], budget)
            if nxt is None:
                return None
            d = {**d, "fault": nxt["fault"], "reason": nxt["reason"], "source": "fallback", "settings": {},
                 "safety_reason": "default strength and the configured safety limits"}
        # Re-clamp on the controller: the agent's own clamping is not trusted.
        final = planner.decide_settings(CATALOG[d["fault"]], d.get("settings"), caps, max_fault_s,
                                        d.get("settings_source", d.get("source", "default")), default_window)
        final["clamped"] = list(dict.fromkeys((d.get("clamped") or []) + final["clamped"]))
        return {**d, **final}

    def predict(self, spec, topology: dict, deterministic: dict) -> dict:
        p = self._call("predict", fault=spec.id, topology=topology, deterministic=deterministic)
        p["predicted_impacted"] = sorted(j for j in p.get("predicted_impacted", []) if j in JOURNEYS)
        return p

    def analyze(self, exp: dict, topology: dict) -> dict:
        return self._call("analyze", exp=exp, topology=topology)

    def suggest_fix(self, exp: dict, current: dict) -> dict:
        return self._call("suggest_fix", exp=exp, current=current)  # the patch is validated by the workflow

    def write_report(self, experiments: list[dict], overall, scorecard: dict, manual: list[dict]) -> dict:
        rep = self._call("write_report", experiments=experiments, overall=overall, scorecard=scorecard, manual=manual)
        # The formula decides order and severity, wherever the Reporter ran.
        ranked = sorted(experiments, key=lambda e: e["scoring"]["severity"], reverse=True)
        by_id = {f.get("id"): f for f in rep.get("ranked_findings", []) if isinstance(f, dict)}
        rep["ranked_findings"] = [{**by_id.get(e["label"], {"id": e["label"]}), "id": e["label"],
                                   "severity": e["scoring"]["severity_label"],
                                   "severity_value": e["scoring"]["severity"]} for e in ranked]
        return rep


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    return str(o)
