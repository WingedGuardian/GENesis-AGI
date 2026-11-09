"""Bind the actual accepted CBM arguments/settings/environment and pressure policy."""

import runpy
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def identity(**changes):
    values = dict(
        snapshot=SimpleNamespace(commit="a" * 40, files={"scripts/start.py": "b" * 64}),
        worker=SimpleNamespace(BUILD="c" * 64),
        config=dict(main="/fixture/repository", cache="/fixture/cache", runtime="/fixture/runtime"),
        args=SimpleNamespace(repo_path="/fixture/repository", mode="full", persistence="true"),
        cap=8 * 1024**3,
        settings=b'{"version":2}',
        environment=dict(PATH="/usr/bin:/bin", CBM_ALLOWED_ROOT="/fixture/repository"),
        policy=dict(route="workload", slice="genesis-workload.slice", wall_seconds=14400,
                    sibling_reserve_bytes=2 * 1024**3, cache_reserve_bytes=2 * 1024**3),
    )
    values.update(changes)
    return runpy.run_path(str(ROOT / "scripts/lib/code_intel_queued_worker.py"))["cbm_source"](**values)


def test_same_effective_inputs_bind_canonically_without_retaining_values():
    first = identity()
    assert first == identity(environment=dict(CBM_ALLOWED_ROOT="/fixture/repository", PATH="/usr/bin:/bin"))
    assert set(first) == {"commit", "files", "provider_digest", "provider_version", "configuration_sha256"}
    assert first["commit"] == "a" * 40 and first["files"] == {"scripts/start.py": "b" * 64}
    assert "/fixture/cache" not in repr(first)


def test_registration_slot_rule_is_an_explicit_source_input():
    original = identity()
    registered = identity(registration_slots={"INVOCATION_ID": "scope.invocation_id"})
    assert registered["configuration_sha256"] != original["configuration_sha256"]
    assert registered["configuration_sha256"] != identity(
        registration_slots={"INVOCATION_ID": "another-source"})["configuration_sha256"]


@pytest.mark.parametrize("field", ["settings", "main", "cache", "runtime", "mode", "persistence",
                                  "repository", "environment", "wall", "route", "sibling", "cache_reserve"])
def test_each_effective_axis_changes_configuration_identity(field):
    original = identity()
    changes = {}
    if field == "settings":
        changes["settings"] = b'{ "version":2}'
    elif field in ("main", "cache", "runtime"):
        config = dict(main="/fixture/repository", cache="/fixture/cache", runtime="/fixture/runtime")
        config[field] += "-other"
        changes["config"] = config
    elif field in ("mode", "persistence", "repository"):
        args = SimpleNamespace(repo_path="/fixture/repository", mode="full", persistence="true")
        setattr(args, {"mode": "mode", "persistence": "persistence", "repository": "repo_path"}[field],
                {"mode": "moderate", "persistence": "false", "repository": "/fixture/other"}[field])
        changes["args"] = args
    elif field == "environment":
        changes["environment"] = dict(PATH="/other/bin", CBM_ALLOWED_ROOT="/fixture/repository")
    else:
        policy = dict(route="workload", slice="genesis-workload.slice", wall_seconds=14400,
                      sibling_reserve_bytes=2 * 1024**3, cache_reserve_bytes=2 * 1024**3)
        if field == "route":
            policy.update(route="inherit", slice="session-fixture.slice")
        else:
            policy[{"wall": "wall_seconds", "sibling": "sibling_reserve_bytes",
                    "cache_reserve": "cache_reserve_bytes"}[field]] += 1
        changes["policy"] = policy
    assert identity(**deepcopy(changes))["configuration_sha256"] != original["configuration_sha256"]
