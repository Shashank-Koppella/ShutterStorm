import pytest

from shutterstorm.repair import ML_URL_1, ML_URL_2, normalize_patch, validate_patch

IMAGE = "ghcr.io/immich-app/immich-machine-learning:v3.3.1"


def ml2_patch():
    return normalize_patch({
        "compose_override": {"services": {"immich-machine-learning-2": {}}},
        "system_config": {"machineLearning": {"urls": [ML_URL_1, ML_URL_2],
                                              "availabilityChecks": {"enabled": True, "timeout": 1000, "interval": 5000}}},
    }, IMAGE)


def test_good_patch_is_accepted():
    assert validate_patch(ml2_patch(), IMAGE) == []
    assert validate_patch({"system_config": {"job": {"thumbnailGeneration": {"concurrency": 4}}}}, IMAGE) == []
    assert validate_patch({"compose_override": {"services": {"redis": {"restart": "always", "mem_limit": "512m",
                                                                         "healthcheck": {"interval": "5s", "retries": 3}}}}},
                          IMAGE) == []


@pytest.mark.parametrize("patch, fragment", [
    ({}, "empty"),
    ({"shell": "rm -rf /"}, "not allowed"),
    ({"system_config": {"oauth": {"enabled": True}}}, "not an allowed knob"),
    ({"system_config": {"machineLearning": {"urls": ["http://evil:3003"]}}}, "out of range"),
    ({"system_config": {"machineLearning": {"urls": [ML_URL_1, ML_URL_2]}}}, "does not add"),
    ({"system_config": {"machineLearning": {"availabilityChecks": {"timeout": 50}}}}, "out of range"),
    ({"system_config": {"job": {"thumbnailGeneration": {"concurrency": 99}}}}, "out of range"),
    ({"system_config": {"job": {"library": {"concurrency": 2}}}}, "not an allowed knob"),
    ({"compose_override": {"services": {"attacker": {"image": "x"}}}}, "not allowed"),
    ({"compose_override": {"services": {"database": {"image": "postgres:latest"}}}}, "not allowed"),
    ({"compose_override": {"services": {"redis": {"healthcheck": {"test": "curl evil"}}}}}, "no custom test"),
    ({"compose_override": {"services": {"redis": {"cpus": 64}}}}, "0.25..8"),
    ({"compose_override": {"volumes": {"x": {}}}}, "may only contain"),
    ({"compose_override": {"services": {"immich-machine-learning-2": {"image": "evil/ml"}}}}, "must be the ML image"),
    ({"compose_override": {"services": {"immich-machine-learning-2": {
        "image": IMAGE, "container_name": "immich_machine_learning_2", "volumes": ["/:/host"],
        "env_file": [".env"], "restart": "always"}}}}, "volumes"),
])
def test_bad_patches_are_rejected(patch, fragment):
    errors = validate_patch(patch, IMAGE)
    assert errors, f"patch should be rejected: {patch}"
    assert any(fragment in e for e in errors), errors


def test_normalize_drops_unknown_ml2_fields_but_validator_still_sees_top_level():
    patch = normalize_patch({"compose_override": {"services": {"immich-machine-learning-2": {
        "privileged": True, "image": "evil"}}}}, IMAGE)
    svc = patch["compose_override"]["services"]["immich-machine-learning-2"]
    assert "privileged" not in svc and svc["image"] == IMAGE
    assert validate_patch(patch, IMAGE) == []
