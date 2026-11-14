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


@pytest.mark.parametrize("enabled,sentinel", [(True, False), (False, False), (True, True)])
def test_cbm_metadata_request_preserves_enablement_sentinel_and_shared_lease(
    selection, tmp_path, monkeypatch, enabled, sentinel
):
    supervisor, request, calls, activity, modules = configured(selection, tmp_path, monkeypatch, 5)
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
