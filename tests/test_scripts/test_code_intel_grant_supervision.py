"""Captured metadata/capacity supervision -> scratch grant, with no provider run."""

import json
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def configured(selection, tmp_path, monkeypatch, version):
    snapshot, modules, environment, cwd, activity, _, _ = selection
    constructor = modules["supervisor"].Supervisor
    assert callable(getattr(constructor, "request_retry", None)), (
        "grant issue must be wired through captured supervision"
    )
    queue = modules["queue"]
    queue._prepare()
    if version == 5:
        with closing(queue._connect()) as db, queue._transaction(db):
            queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(snapshot.root), "gitnexus", "full")
    calls = []

    @contextmanager
    def repository(modules, repo, h):
        assert repo == str(snapshot.root) and h == queue.marker_hash(repo)
        calls.append("repository")
        yield 9

    class Manager:
        def __init__(self, *args, **kwargs):
            pass

        def require_no_batch(self, physical):
            calls.append("global")

        def owner_slice(self, pid):
            return "app-capped.slice"

        def require_slice_capacity(self, name, observe):
            assert name == "app-capped.slice"
            calls.append("capacity")
            observe("/app-capped.slice")

    root = tmp_path / "capacity"
    group = root / "app-capped.slice"
    group.mkdir(parents=True)
    (root / "memory.max").write_text("max")
    (group / "memory.max").write_text(str(16 * 1024**3))
    (group / "memory.current").write_text("0")
    monkeypatch.setattr(modules["owner_admission"], "repository_lock_for", repository)
    monkeypatch.setattr(modules["scope"], "ScopeManager", Manager)
    monkeypatch.setattr(modules["unit"], "resolve_control_group", lambda control: (group, root, 2))
    monkeypatch.setattr(modules["unit"], "systemctl_env", lambda: dict(environment))
    monkeypatch.setattr(modules["admission"], "_host_available", lambda path: 64 * 1024**3)
    monkeypatch.setenv("CODE_INTEL_INDEX_DISABLE", "0")
    monkeypatch.setenv("CODE_INTEL_WORKLOAD_SLICE", "0")
    monkeypatch.chdir(cwd)
    supervisor = constructor(snapshot, modules, lambda: calls.append("runner"))
    request = dict(
        repo_path=str(snapshot.root),
        provider="gitnexus",
        origin="pending",
        run_mode="full",
        not_after_wall_ns=time.time_ns() + 3600 * 10**9,
        evidence_reference="fixture://supervised-run",
        managed_config="/unused",
    )
    return supervisor, request, calls, activity, modules


@pytest.mark.parametrize("version", [4, 5])
def test_explicit_issue_uses_source_metadata_and_capacity_without_claiming_or_executing(
    selection, tmp_path, monkeypatch, version
):
    supervisor, request, calls, activity, modules = configured(selection, tmp_path, monkeypatch, version)
    queue = modules["queue"]
    snapshot = supervisor.snapshot
    if version == 4:
        before = queue.database_path().read_bytes()
        with pytest.raises(RuntimeError, match="schema5"):
            supervisor.request_retry(request)
        assert queue.database_path().read_bytes() == before
    else:
        grant = supervisor.request_retry(request)
        assert grant["status"] == "issued" and grant["source"]["files"] == snapshot.files
        assert grant["source"]["commit"] == snapshot.commit
        assert grant["source"]["provider_version"] == "gitnexus@1.6.12"
        assert calls == ["runner", "repository", "global", "capacity"]
        with closing(queue._connect()) as db:
            assert modules["retry"].load(db, grant["hash"], "gitnexus", queue._native) == grant
            assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0
    assert not activity.exists()
    assert {row["attempts"] for row in queue.list_markers()} == {0}


def cbm_configuration(supervisor, request, modules, monkeypatch, *, enabled=True, sentinel=False):
    """Existing accepted-config/lease fixture shared by metadata and selected cases."""
    queue, managed, root = modules["queue"], modules["managed"], supervisor.snapshot.root
    queue.write_marker(str(root), "cbm", "full")
    config = dict(version=2, main=str(root), binary=str(root / "synthetic-binary"),
                  cache=str(root / "cache"), runtime=str(root / "runtime"),
                  sentinel=str(root / "disabled"), build=modules["worker"].BUILD)
    settings = root / "settings.json"
    settings.write_text(json.dumps(config))
    if sentinel:
        Path(config["sentinel"]).touch()
    held, streams, observed = [], [], []

    @contextmanager
    def lifecycle(*, shared):
        assert shared is True
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    @contextmanager
    def verified(path):
        assert path == Path(config["binary"]) and held == [True]
        with open("/dev/null", "rb") as stream:
            streams.append(stream)
            yield stream

    original_issue = queue._retry.issue

    def issue(*args, **kwargs):
        assert held == [True] and streams and all(stream.closed for stream in streams)
        observed.append(True)
        return original_issue(*args, **kwargs)

    monkeypatch.setattr(managed, "lifecycle_lock", lifecycle)
    monkeypatch.setattr(managed, "verified_binary", verified)
    monkeypatch.setattr(managed, "show", lambda *args, **kwargs: {"UnitFileState": "enabled" if enabled else "disabled"})
    monkeypatch.setattr(queue._retry, "issue", issue)
    request.update(provider="cbm", managed_config=str(settings))
    return held, streams, observed


@pytest.mark.parametrize("enabled,sentinel", [(True, False), (False, False), (True, True)])
def test_cbm_metadata_request_preserves_enablement_sentinel_and_shared_lease(
    selection, tmp_path, monkeypatch, enabled, sentinel
):
    supervisor, request, calls, activity, modules = configured(selection, tmp_path, monkeypatch, 5)
    queue = modules["queue"]
    held, streams, observed = cbm_configuration(
        supervisor, request, modules, monkeypatch, enabled=enabled, sentinel=sentinel)
    before = queue.database_path().read_bytes()
    if enabled and not sentinel:
        grant = supervisor.request_retry(request)
        assert grant["provider"] == "cbm" and grant["source"]["files"] == supervisor.snapshot.files
        assert observed == [True]
    else:
        with pytest.raises(ValueError, match="sentinel|persistently enabled"):
            supervisor.request_retry(request)
        assert queue.database_path().read_bytes() == before
        assert not observed
    assert not held and all(stream.closed for stream in streams)
    assert not activity.exists()
    with closing(queue._connect()) as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 0


@pytest.mark.parametrize("lost_ack", [False, True])
def test_selected_supervisor_runs_only_its_first_acknowledged_actual_owner(
    selection, tmp_path, monkeypatch, lost_ack
):
    supervisor, request, calls, activity, modules = configured(selection, tmp_path, monkeypatch, 5)
    queue = modules["queue"]
    operation = getattr(supervisor, "claim_selected", None)
    assert callable(operation), "selected runtime must reach the same supervised actual owner"
    grant = supervisor.request_retry(request)
    executions = []

    class Owner:
        def __init__(self, used, context, reserved, probe, manager):
            assert used is modules and reserved["owner"] == queue._recovery.owner_identity()
            self.context = context

        def run(self, bound):
            executions.append(self.context)
            assert bound.runtime_value["context"] == self.context
            assert queue.execution_status(self.context)["launch_permission"] == "reserved"
            return None  # No workload or fabricated completion; original control stays open.

    monkeypatch.setattr(modules["owner"], "PhysicalOwner", Owner)
    data = dict(repo_path=request["repo_path"], provider="gitnexus", nonce=grant["nonce"],
                managed_config="/unused")
    original = queue._decision_gate
    if lost_ack:
        @contextmanager
        def lost(*args, **kwargs):
            with original(*args, **kwargs) as admitted:
                yield admitted
            raise OSError("synthetic lost committed acknowledgement")

        monkeypatch.setitem(queue.claim_native.__globals__, "_decision_gate", lost)
        with pytest.raises(OSError, match="lost committed"):
            operation(data)
        monkeypatch.setitem(queue.claim_native.__globals__, "_decision_gate", original)
    else:
        first = operation(data)
        assert first["launch_authority"] is False and first["state"] == "open"
    duplicate = operation(data)
    assert duplicate["launch_authority"] is False
    # The read-only preflight is advisory: the transaction must supply the
    # actual already-consumed context even if that earlier read saw issued.
    monkeypatch.setattr(supervisor, "_current_grant", lambda h, provider: grant)
    stale_read = operation(data)
    assert stale_read["trial"] == duplicate["trial"] and stale_read["launch_authority"] is False
    assert len(executions) == (0 if lost_ack else 1)
    with closing(queue._connect()) as db:
        retained = queue._retry.load(db, grant["hash"], "gitnexus", queue._native)
        assert retained["status"] == "spent" and duplicate["trial"] == retained["trial"]
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM inflight").fetchone()[0] == 1
        assert queue._native.execution(db, retained["trial"], queue._legs)["native_intent"] is None
    assert not activity.exists()
    entered = []

    def forbidden(*args, **kwargs):
        entered.append(True)
        raise AssertionError("ordinary owner reached selected metadata")

    admission = modules["owner_admission"]
    policy = admission.policy(modules, "gitnexus", grant["run_mode"],
                              modules["scope"].ScopeManager(), {})
    monkeypatch.setattr(admission, "prepared_inputs", forbidden)
    with pytest.raises(ValueError, match="selected"):
        admission.reserve_runtime(supervisor.snapshot, modules, duplicate["trial"],
                                  grant["run_mode"], "/unused", policy,
                                  actual_owner=queue._recovery.owner_identity())
    assert not entered
