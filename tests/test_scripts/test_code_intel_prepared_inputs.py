"""Prospective metadata binds a real future unit, never a fabricated execution."""

import runpy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_owner_admission import runtime_case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


def prepare(namespace):
    method = namespace.get("prepared_inputs")
    assert callable(method), "issue preparation cannot require a fake sequence/owner reservation"
    return method


def test_cbm_metadata_keeps_lifecycle_lease_and_never_enters_queue(queue, tmp_path, monkeypatch):
    namespace, snapshot, modules, claimed, settings, selected, streams = runtime_case(
        queue, tmp_path, monkeypatch
    )
    method = prepare(namespace)
    context = claimed["context"]
    future = modules["native"].expected_unit({**context, "claim_id": "b" * 32})
    held = []

    @contextmanager
    def lease(*, shared):
        assert shared and not held
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "prospective input capture has no queue admission or reservation authority"
        )

    monkeypatch.setattr(modules["managed"], "lifecycle_lock", lease)
    monkeypatch.setattr(modules["queue"], "_connect", forbidden)
    monkeypatch.setattr(modules["queue"], "reserve_launch", forbidden)
    with method(
        snapshot, modules, context["repo_path"], "cbm", "fast", str(settings), selected, unit=future
    ) as value:
        assert held == [True] and streams[-1].closed
        assert set(value) == {"source", "provider_options", "environment_recipe"}
        assert value["source"]["commit"] == snapshot.commit
        assert value["environment_recipe"]["frozen"]["CODE_INTEL_CHILD_SCOPE_UNIT"] == future
        assert "owner" not in value and "context" not in value and "reservation" not in value
        assert value["provider_options"]["managed_config"] == str(settings)
    assert not held


def test_gn_metadata_needs_no_claim_or_cbm_settings_and_executes_nothing(selection, monkeypatch):
    snapshot, modules, environment, cwd, activity, _, _ = selection
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_owner_admission.py")
    )
    method = prepare(namespace)
    context = dict(
        hash=modules["queue"].marker_hash(str(snapshot.root)),
        provider="gitnexus",
        claim_id="b" * 32,
    )
    future = modules["queue"]._native.expected_unit(context)
    selected = namespace["policy"](modules, "gitnexus", "fast", SimpleNamespace(), {})
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(modules["unit"], "systemctl_env", lambda: dict(environment))

    def forbidden(*args, **kwargs):
        raise AssertionError("GN metadata must not enter queue or CBM lifecycle")

    monkeypatch.setattr(modules["queue"], "_connect", forbidden)
    monkeypatch.setattr(modules["managed"], "lifecycle_lock", forbidden)
    with method(
        snapshot, modules, str(snapshot.root), "gitnexus", "fast", "/unused", selected, unit=future
    ) as value:
        assert value["provider_options"]["cwd"] == str(cwd)
        assert value["environment_recipe"]["frozen"]["CODE_INTEL_CHILD_SCOPE_UNIT"] == future
        assert value["source"]["provider_digest"] == value["provider_options"]["node"]["sha256"]
    assert not activity.exists()
