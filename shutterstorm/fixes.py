"""Fix suggestions: schema, fail-closed validator and human-readable rendering.

ShutterStorm never applies a fix. The Fix Advisor (agents.suggest_fix) proposes one, this
module checks that it only uses known, bounded settings (so a human could apply it safely)
and renders it as a diff plus manual steps. A suggestion has two optional parts::

    {"system_config": {...partial Immich AdminConfigDto...},
     "compose_override": {"services": {...}}}

Only the knobs in :data:`KNOBS` pass validation. A suggestion that fails is still shown,
labelled with the reasons, so the reader can see why it is not recommended.
"""
from __future__ import annotations

import copy
import json
import re

from .config import ML2, OVERRIDE_FILE

ML_URL_1 = "http://immich-machine-learning:3003"
ML_URL_2 = "http://immich-machine-learning-2:3003"
ALLOWED_ML_URLS = [ML_URL_1, ML_URL_2]
JOB_QUEUES = ["thumbnailGeneration", "metadataExtraction", "smartSearch", "faceDetection", "videoConversion"]
ML2_SERVICE = "immich-machine-learning-2"
EXISTING_SERVICES = ["immich-server", "immich-machine-learning", "redis", "database"]
ML2_FIXED = {"container_name": ML2, "volumes": ["model-cache:/cache"], "env_file": [".env"], "restart": "always"}
ML2_ENV_KEYS = {"MACHINE_LEARNING_PRELOAD__CLIP__TEXTUAL"}
RESTART_VALUES = {"always", "unless-stopped", "on-failure"}
DURATION_RE = re.compile(r"^\d{1,4}(ms|s|m)$")
MEM_RE = re.compile(r"^\d{2,5}[mMgG]$")

# Human/LLM-readable description of every allowed knob.
KNOBS = {
    "system_config": {
        "machineLearning.availabilityChecks.enabled": "bool",
        "machineLearning.availabilityChecks.timeout": "int ms, 200..10000",
        "machineLearning.availabilityChecks.interval": "int ms, 1000..60000",
        "machineLearning.urls": f"non-empty list, values only from {ALLOWED_ML_URLS}; "
                                f"{ML_URL_2} requires the {ML2_SERVICE} service",
        **{f"job.{q}.concurrency": "int 1..8" for q in JOB_QUEUES},
    },
    "compose_override": {
        f"services.{ML2_SERVICE}": "add a second ML instance (same image, shares model-cache; "
                                   "needs ~1-2 GB extra RAM). Optional environment key: "
                                   "MACHINE_LEARNING_PRELOAD__CLIP__TEXTUAL=<clip model name>",
        **{f"services.{s}": "only keys restart (always|unless-stopped|on-failure), mem_limit (e.g. '2g'), "
                            "cpus (0.25..8), healthcheck {interval, timeout, start_period (e.g. '10s'), retries 1..10}"
           for s in EXISTING_SERVICES},
    },
}


def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            out.update(_flatten(v, path))
        else:
            out[path] = v
    return out


def _check_int(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _validate_system_config(sc: dict) -> list[str]:
    errors = []
    for path, value in _flatten(sc).items():
        if path == "machineLearning.availabilityChecks.enabled":
            ok = isinstance(value, bool)
        elif path == "machineLearning.availabilityChecks.timeout":
            ok = _check_int(value, 200, 10000)
        elif path == "machineLearning.availabilityChecks.interval":
            ok = _check_int(value, 1000, 60000)
        elif path == "machineLearning.urls":
            ok = (isinstance(value, list) and value and len(set(value)) == len(value)
                  and all(u in ALLOWED_ML_URLS for u in value))
        elif re.fullmatch(r"job\.(\w+)\.concurrency", path) and path.split(".")[1] in JOB_QUEUES:
            ok = _check_int(value, 1, 8)
        else:
            errors.append(f"system_config.{path} is not an allowed knob")
            continue
        if not ok:
            errors.append(f"system_config.{path}={value!r} is out of range ({KNOBS['system_config'][path]})")
    return errors


def _validate_healthcheck(hc, where: str) -> list[str]:
    if not isinstance(hc, dict):
        return [f"{where}.healthcheck must be an object"]
    errors = []
    for k, v in hc.items():
        if k in ("interval", "timeout", "start_period"):
            if not (isinstance(v, str) and DURATION_RE.match(v)):
                errors.append(f"{where}.healthcheck.{k}={v!r} must look like '10s'")
        elif k == "retries":
            if not _check_int(v, 1, 10):
                errors.append(f"{where}.healthcheck.retries must be 1..10")
        else:
            errors.append(f"{where}.healthcheck.{k} is not allowed (no custom test commands)")
    return errors


def _validate_compose(override: dict, ml_image: str) -> list[str]:
    errors = []
    if set(override) - {"services"}:
        errors.append(f"compose_override may only contain 'services' (got {sorted(override)})")
    services = override.get("services", {})
    if not isinstance(services, dict):
        return errors + ["compose_override.services must be an object"]
    for name, svc in services.items():
        where = f"compose_override.services.{name}"
        if not isinstance(svc, dict):
            errors.append(f"{where} must be an object")
            continue
        if name == ML2_SERVICE:
            allowed = {"image", "container_name", "volumes", "env_file", "restart", "environment"}
            for k in set(svc) - allowed:
                errors.append(f"{where}.{k} is not allowed")
            if svc.get("image") != ml_image:
                errors.append(f"{where}.image must be the ML image {ml_image}")
            for k, v in ML2_FIXED.items():
                if svc.get(k) != v:
                    errors.append(f"{where}.{k} must be {v!r}")
            env = svc.get("environment", {})
            if not isinstance(env, dict) or set(env) - ML2_ENV_KEYS or \
                    any(not re.fullmatch(r"[\w.\-]+", str(v)) for v in env.values()):
                errors.append(f"{where}.environment may only set {sorted(ML2_ENV_KEYS)} to a model name")
        elif name in EXISTING_SERVICES:
            for k, v in svc.items():
                if k == "restart":
                    if v not in RESTART_VALUES:
                        errors.append(f"{where}.restart must be one of {sorted(RESTART_VALUES)}")
                elif k == "mem_limit":
                    if not (isinstance(v, str) and MEM_RE.match(v)):
                        errors.append(f"{where}.mem_limit={v!r} must look like '2g' or '512m'")
                elif k == "cpus":
                    if not (isinstance(v, (int, float)) and not isinstance(v, bool) and 0.25 <= v <= 8):
                        errors.append(f"{where}.cpus must be 0.25..8")
                elif k == "healthcheck":
                    errors += _validate_healthcheck(v, where)
                else:
                    errors.append(f"{where}.{k} is not allowed")
        else:
            errors.append(f"service {name!r} is not allowed (only {ML2_SERVICE} or {EXISTING_SERVICES})")
    return errors


def normalize_patch(patch: dict, ml_image: str) -> dict:
    """Fill the fixed fields of the second ML service; everything else is left as proposed."""
    patch = copy.deepcopy(patch or {})
    patch = {k: v for k, v in patch.items() if v not in (None, {}, [])}
    services = patch.get("compose_override", {}).get("services", {})
    if isinstance(services, dict) and isinstance(services.get(ML2_SERVICE), dict):
        services[ML2_SERVICE] = {"image": ml_image, **ML2_FIXED,
                                 **{k: v for k, v in services[ML2_SERVICE].items() if k == "environment"}}
    return patch


def validate_patch(patch: dict, ml_image: str, ml2_present: bool = False) -> list[str]:
    """Fail-closed validation. Returns a list of errors (empty = valid)."""
    if not isinstance(patch, dict) or not patch:
        return ["patch is empty"]
    errors = [f"top-level key {k!r} is not allowed" for k in set(patch) - {"system_config", "compose_override"}]
    sc = patch.get("system_config")
    co = patch.get("compose_override")
    if not sc and not co:
        errors.append("patch changes nothing")
    if sc is not None:
        errors += _validate_system_config(sc) if isinstance(sc, dict) else ["system_config must be an object"]
    if co is not None:
        errors += _validate_compose(co, ml_image) if isinstance(co, dict) else ["compose_override must be an object"]
    urls = (sc or {}).get("machineLearning", {}).get("urls", []) if isinstance(sc, dict) else []
    adds_ml2 = isinstance(co, dict) and ML2_SERVICE in (co.get("services") or {})
    if ML_URL_2 in (urls or []) and not (adds_ml2 or ml2_present):
        errors.append(f"machineLearning.urls uses {ML_URL_2} but the patch does not add {ML2_SERVICE}")
    if adds_ml2 and ML_URL_2 not in (urls or []):
        errors.append(f"adds {ML2_SERVICE} but does not list {ML_URL_2} in machineLearning.urls, "
                      f"so Immich would never use it")
    return errors


def ml_image(ctx) -> str:
    """The image of the existing ML service, from the compose config."""
    compose = ctx.docker.compose_config(ctx.settings.immich_dir)
    return compose["services"]["immich-machine-learning"]["image"]


def describe_patch(patch: dict, current_config: dict | None = None) -> str:
    """Human-readable diff of a patch."""
    lines = []
    for path, value in _flatten(patch.get("system_config") or {}).items():
        old = current_config
        for part in path.split("."):
            old = old.get(part) if isinstance(old, dict) else None
        lines.append(f"system-config  {path}: {json.dumps(old)} -> {json.dumps(value)}")
    co = patch.get("compose_override")
    if co:
        lines.append(f"compose override ({OVERRIDE_FILE}):")
        lines += ["  " + line for line in json.dumps(co, indent=2).splitlines()]
    return "\n".join(lines) or "(empty patch)"


def manual_steps(patch: dict) -> list[str]:
    """How a human could apply a (valid) suggestion by hand. ShutterStorm never does this itself."""
    steps = []
    if patch.get("compose_override"):
        steps.append(f"Save this as {OVERRIDE_FILE} next to Immich's docker-compose.yml:\n"
                     + json.dumps(patch["compose_override"], indent=2))
        steps.append(f"Run in the Immich folder: docker compose -f docker-compose.yml -f {OVERRIDE_FILE} up -d")
    if patch.get("system_config"):
        steps.append("Change these values in Immich's admin settings, or merge this JSON into the full config "
                     "from GET /api/system-config and send it back with PUT /api/system-config:\n"
                     + json.dumps(patch["system_config"], indent=2))
    steps.append("Re-run the same experiment (python -m shutterstorm run --experiments <fault>) to measure the effect.")
    return steps
