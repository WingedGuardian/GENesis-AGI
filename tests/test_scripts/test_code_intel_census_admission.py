"""Owner-call ordering with synthetic locks/manager; no DB, provider or scope."""

import json
import runpy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


@pytest.mark.parametrize("provider", ["cbm", "gitnexus"])
@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.parametrize("birth_drift", [False, True])
def test_global_absence_is_required_under_locks_before_any_runtime_reservation(
    tmp_path, monkeypatch, provider, refused, birth_drift
):
    _, _, snapshot = case(tmp_path, monkeypatch, "runner")
    modules = dict(snapshot.assemble())
    context = runtime(snapshot, modules)["context"]
    context["provider"] = provider
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_owner_admission.py")
    )
    namespace = namespace["main"].__globals__
    calls = []

    @contextmanager
    def lock(*args):
        calls.append("lock")
        try:
            yield
        finally:
            calls.append("unlock")

    class Manager:
        def __init__(self, *args, deadline):
            pass

        def require_no_batch(self, physical_reader):
            assert calls == ["lock"] and physical_reader is modules["unit"].cgroup_empty
            calls.append("global")
            if refused:
                raise ValueError("synthetic unresolved old batch")
            if birth_drift:
                owner = modules["queue"]._recovery.owner_identity()
                monkeypatch.setattr(modules["queue"]._recovery, "owner_identity",
                    lambda: {**owner, "start_ticks": owner["start_ticks"] + 1})

    def reserve(*args, actual_owner):
        assert actual_owner == modules["queue"]._recovery.owner_identity()
        calls.append("metadata")
        raise ValueError("controlled stop before metadata or reservation")

    monkeypatch.setitem(namespace, "repository_lock", lock)
    monkeypatch.setitem(namespace, "reserve_runtime", reserve)
    modules["scope"] = SimpleNamespace(ScopeManager=Manager)
    monkeypatch.setenv("CODE_INTEL_WORKLOAD_SLICE", "1")
    monkeypatch.setenv("CODE_INTEL_INDEX_DISABLE", "0")
    assert (
        namespace["main"](snapshot, modules, json.dumps(context), "fast", "/unused/settings") == 76
    )
    assert calls == (
        ["lock", "global", "unlock"] if refused or birth_drift
        else ["lock", "global", "metadata", "unlock"]
    )
    assert not (tmp_path / "state/index-requests").exists()
