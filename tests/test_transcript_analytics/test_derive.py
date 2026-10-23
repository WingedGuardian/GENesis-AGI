"""Disposable snapshots, complete reads, and history-only fresh restore."""

import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import derive, query, store


def _write(path, number):
    path.write_text(json.dumps({
        "type": "assistant", "uuid": f"event-{number}", "sessionId": "session",
        "timestamp": "2026-01-01T00:00:00Z", "message": {
            "id": f"message-{number}", "model": "fixture", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": f"tool-{number}", "name": "Bash",
                         "input": {"command": f"echo {number}"}}],
            "usage": {"input_tokens": 1, "output_tokens": number},
        },
    }) + "\n")


@pytest.fixture
def built(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "source.jsonl"
    _write(path, 1)
    assert store.ingest(projects, data)["rebuilt"] == 1
    derive.build(data)
    return projects, data, path


def _rows(data, *, live):
    with query.read_connection(data, live=live) as con:
        return {
            view: (con.sql(f"SELECT * FROM {view}").columns,
                   con.sql(f"SELECT * FROM {view} ORDER BY ALL").fetchall())
            for view in derive.DERIVED_VIEWS
        }


def test_snapshot_equals_every_live_view(built):
    _projects, data, _path = built
    assert derive.is_current(data)
    assert derive.build(data, if_stale=True) == {"derived": "current; nothing to do"}
    assert _rows(data, live=False) == _rows(data, live=True)
    with query.read_connection(data) as con:
        assert query._READS[con]["snapshot"]["views_version"] == query.VIEWS_VERSION


def test_snapshot_matches_shared_current_and_historical_corpus(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    fixture = Path(__file__).with_name("fixtures") / "flat-v1"
    shutil.copytree(fixture, data)
    for name, number in [("session", 1), ("copy", 1), ("other", 2)]:
        _write(projects / f"{name}.jsonl", number)
    assert store.ingest(projects, data, adopt_projects_root=True)["rebuilt"] == 3
    live = _rows(data, live=True)
    assert live["tool_calls"][1] and live["turns"][1] and live["sessions"][1]
    assert len(store.select_sources(data)[0]) == 4
    shutil.rmtree(projects)
    derive.build(data)
    assert _rows(data, live=False) == live
    with query.read_connection(data) as con:
        assert query._READS[con]["snapshot"]["coverage"]["historical"][0]["adapter"] == "flat-v1"


@pytest.mark.parametrize("fault", [
    "missing", "corrupt", "read_error", "schema", "manifest", "version", "signature",
    "dangling", "loop", "escaping",
])
def test_every_snapshot_open_or_read_error_falls_back_once(built, capsys, fault):
    _projects, data, _path = built
    current = data / "derived/current"
    table = current / "tool_calls.parquet"
    manifest_path = current / "MANIFEST.json"
    if fault == "missing":
        table.unlink()
    elif fault == "corrupt":
        table.write_bytes(b"not parquet")
    elif fault == "read_error":
        blob = bytearray(table.read_bytes())
        blob[4:32] = b"\xff" * 28
        table.write_bytes(blob)
        assert pq.read_metadata(table).num_rows == 1  # footer remains readable
    elif fault == "schema":
        shutil.copy2(current / "turns.parquet", table)
    elif fault == "manifest":
        manifest_path.write_text("broken json")
    elif fault in {"version", "signature"}:
        manifest = json.loads(manifest_path.read_text())
        manifest["views_version" if fault == "version" else "inputs_signature"] = "wrong"
        manifest_path.write_text(json.dumps(manifest))
    else:
        current.unlink()
        current.symlink_to({
            "dangling": "absent", "loop": "current", "escaping": "../..",
        }[fault])
    capsys.readouterr()
    assert query.run_query(data, "SELECT output_tokens FROM turns")[1] == [(1,)]
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert sum(event.get("event") == "snapshot_fallback" for event in events) == 1
    assert events[-1]["mode"] == "live"


def test_snapshot_is_stale_after_ingest_and_prune(built):
    projects, data, path = built
    _write(path, 2)
    store.ingest(projects, data)
    assert not derive.is_current(data)
    assert query.run_query(data, "SELECT output_tokens FROM turns")[1] == [(2,)]
    derive.build(data)
    assert derive.is_current(data)
    path.unlink()
    assert store.prune(data, before="2027-01-01", projects=projects) == 1
    assert not derive.is_current(data)
    assert query.run_query(data, "SELECT count(*) FROM turns")[1] == [(0,)]


def test_opened_snapshot_survives_two_newer_builds_and_old_cache_cleanup(built):
    _projects, data, _path = built
    first = (data / "derived/current").resolve()
    with query.read_connection(data) as reader:
        second = Path(derive.build(data)["dir"])
        third = Path(derive.build(data)["dir"])
        assert not first.exists()
        assert second.exists() and third.exists()
        assert os.readlink(data / "derived/current") == third.name
        assert reader.sql("SELECT output_tokens FROM turns").fetchall() == [(1,)]


def test_build_failure_keeps_old_snapshot_but_queries_use_fresh_live(built, monkeypatch):
    projects, data, path = built
    old = (data / "derived/current").resolve()
    original = (old / "MANIFEST.json").read_bytes()
    _write(path, 2)
    store.ingest(projects, data)
    connect = query.connect

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(query, "connect", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        derive.build(data)
    assert (data / "derived/current").resolve() == old
    assert (old / "MANIFEST.json").read_bytes() == original
    monkeypatch.setattr(query, "connect", connect)
    assert query.run_query(data, "SELECT output_tokens FROM turns")[1] == [(2,)]


def test_build_sweeps_killed_staging_and_temporary_links(built):
    _projects, data, _path = built
    root = data / "derived"
    (root / ".staging/dead").mkdir(parents=True)
    (root / ".staging/dead/body").write_bytes(b"incomplete")
    (root / ".current.424242").symlink_to("absent")
    derive.build(data)
    staging = root / ".staging"
    assert not staging.exists() or not list(staging.iterdir())
    assert not (root / ".current.424242").is_symlink()


def test_build_empty_store_and_normalized_prune(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    store.ingest(projects, data)
    derive.build(data)
    assert query.run_query(data, "SELECT count(*) FROM turns")[1] == [(0,)]
    assert store.prune(data, before="20260101", projects=projects) == 0


@pytest.mark.parametrize("format_name", ["flat-v1", "record-v5"])
def test_history_live_snapshot_and_fresh_archive_restore_without_originals(tmp_path, format_name):
    fixture = Path(__file__).parent / "fixtures" / format_name
    data = tmp_path / "original"
    shutil.copytree(fixture, data, symlinks=True)
    live = _rows(data, live=True)
    assert live["turns"][1] and live["tool_calls"][1]
    derive.build(data)
    assert _rows(data, live=False) == live
    archive = tmp_path / "backup.tar"
    with tarfile.open(archive, "w") as output:
        output.add(data, arcname="store")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with tarfile.open(archive) as incoming:
        incoming.extractall(fresh, filter="data")
    restored = fresh / "store"
    durable = {
        str(path.relative_to(restored)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in restored.rglob("*") if path.is_file() and "derived" not in path.parts
    }
    shutil.rmtree(data)
    derive.discard_after_restore(restored)
    assert not (restored / "derived").exists()
    assert durable == {
        str(path.relative_to(restored)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in restored.rglob("*") if path.is_file()
    }
    assert _rows(restored, live=True) == live
    derive.build(restored)
    assert _rows(restored, live=False) == live
    with query.read_connection(restored) as con:
        historical = query._READS[con]["coverage"]["historical"]
        assert historical[0]["adapter"] == format_name
        assert historical[0]["unknown"]["unrecoverable"]


def test_snapshot_preserves_reconciliation_query_surface(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    _write(projects / "source.jsonl", 1)
    assert store.ingest(projects, data)["rebuilt"] == 1
    before = {
        view: query.run_query(data, f"SELECT count(*) FROM {view}")[1]
        for view in ("fragments", "spawn_associations", "tool_ownership")
    }
    derive.build(data)
    after = {
        view: query.run_query(data, f"SELECT count(*) FROM {view}")[1]
        for view in before
    }
    assert after == before
    with query.read_connection(data) as con:
        assert query._READS[con]["snapshot"] is not None
        assert con.sql("SELECT count(*) FROM fragments").fetchone() == (1,)


def test_if_stale_noop_still_sweeps_interrupted_artifacts(built):
    """Codex: sweep interrupted artifacts before the early return."""
    _projects, data, _path = built
    root = data / "derived"
    (root / ".staging" / "torn").mkdir(parents=True)
    (root / ".current.999").symlink_to("torn")
    (root / "orphaned-snapshot").mkdir()
    assert derive.is_current(data)
    assert derive.build(data, if_stale=True) == {"derived": "current; nothing to do"}
    remaining = {entry.name for entry in root.iterdir()}
    assert ".staging" not in remaining
    assert not list(root.glob(".current.*"))
    assert "orphaned-snapshot" not in remaining


def test_non_symlink_snapshot_selector_is_rejected_and_falls_back(built):
    """Codex: reject non-symlink snapshot selectors."""
    _projects, data, _path = built
    root = data / "derived"
    selector = root / "current"
    target = selector.resolve()
    selector.unlink()
    target.rename(selector)
    assert selector.is_dir() and not selector.is_symlink()
    assert not derive.is_current(data)
    with query.read_connection(data) as con:
        assert query._READS[con].get("snapshot") is None
        assert con.sql("SELECT count(*) FROM tool_calls").fetchall()


def test_symlink_derived_root_is_discarded_on_build(built):
    """Codex: a symlinked derived root is malformed and must not be used."""
    _projects, data, _path = built
    root = data / "derived"
    moved = data / "moved-derived"
    root.rename(moved)
    root.symlink_to(moved.name)
    assert not derive.is_current(data)
    derive.build(data)
    assert root.is_dir() and not root.is_symlink()
    assert derive.is_current(data)


def test_raw_views_stay_forbidden_when_fallback_serves_live_graph(built):
    """Codex: enforce live mode for raw views after snapshot fallback."""
    _projects, data, _path = built
    shutil.rmtree(data / "derived")
    with pytest.raises(ValueError, match="require --live"):
        query.run_query(data, "SELECT count(*) FROM raw_tool_calls", live=False)
    columns, rows = query.run_query(data, "SELECT count(*) FROM raw_tool_calls", live=True)
    assert rows


def test_malformed_layout_is_swept_before_rebuild(built):
    """Codex: discard invalid disposable layouts before rebuilding."""
    _projects, data, _path = built
    root = data / "derived"
    selector = root / "current"
    target = selector.resolve()
    selector.unlink()
    target.rename(selector)
    (root / ".staging" / "torn").mkdir(parents=True)
    (root / "orphaned").mkdir()
    result = derive.build(data, if_stale=True)
    assert result["dir"].endswith((root / "current").resolve().name)
    names = {entry.name for entry in root.iterdir()}
    assert ".staging" not in names and "orphaned" not in names
    assert (root / "current").is_symlink()
    assert derive.is_current(data)


@pytest.mark.parametrize("entry,kind", [
    (".staging", "file"), (".staging", "link"), (".staging", "dangling"),
    (".current.424242", "directory"), (".current.424242", "file"),
    (".current.424242", "link"), (".", "file"),
])
def test_rebuild_recovers_malformed_cache_entry_types(built, tmp_path, entry, kind):
    _projects, data, _path = built
    root = data / "derived"
    target = root if entry == "." else root / entry
    if target.is_dir():
        shutil.rmtree(target)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    sentinel = outside / "retained"
    sentinel.write_text("keep")
    if kind == "directory":
        target.mkdir()
        (target / "partial").write_text("interrupted")
    elif kind in {"link", "dangling"}:
        target.symlink_to(outside if kind == "link" else outside / "absent")
    else:
        target.write_text("malformed")
    derive.build(data, if_stale=True)
    assert derive.is_current(data)
    assert sentinel.read_text() == "keep"
    assert not (root / ".staging").exists()
    assert not (root / ".staging").is_symlink()
    assert not (root / ".current.424242").exists()
    with query.read_connection(data) as con:
        assert con.sql("SELECT output_tokens FROM turns").fetchall() == [(1,)]


@pytest.mark.parametrize("kind", ["directory", "file", "link", "dangling"])
def test_restore_cache_discard_syncs_parent_after_removal(tmp_path, monkeypatch, kind):
    data = tmp_path / "data"
    data.mkdir()
    root = data / "derived"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "retained").write_text("keep")
    if kind == "directory":
        root.mkdir()
    elif kind in {"link", "dangling"}:
        root.symlink_to(outside if kind == "link" else outside / "absent")
    else:
        root.write_text("malformed")
    synced = []
    fsync = store._fsync_directory

    def record_sync(path):
        assert not root.exists() and not root.is_symlink()
        synced.append(path)
        fsync(path)

    monkeypatch.setattr(store, "_fsync_directory", record_sync)
    derive.discard_after_restore(data, lock_path=tmp_path / "writer.lock")
    assert synced == [data]
    assert (outside / "retained").read_text() == "keep"


def test_restore_cache_discard_reports_failed_parent_sync(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "derived").mkdir(parents=True)

    def fail_sync(_path):
        raise OSError("synthetic directory sync failure")

    monkeypatch.setattr(store, "_fsync_directory", fail_sync)
    with pytest.raises(OSError, match="synthetic directory sync failure"):
        derive.discard_after_restore(data, lock_path=tmp_path / "writer.lock")


@pytest.mark.parametrize("failures", [1, 2])
def test_restore_cache_discard_retries_failed_parent_sync(tmp_path, monkeypatch, failures):
    data = tmp_path / "data"
    root = data / "derived"
    root.mkdir(parents=True)
    calls = []
    fsync = store._fsync_directory

    def retry_sync(path):
        assert not root.exists() and not root.is_symlink()
        calls.append(path)
        if len(calls) <= failures:
            raise OSError("synthetic directory sync failure")
        fsync(path)

    monkeypatch.setattr(store, "_fsync_directory", retry_sync)
    for _ in range(failures):
        with pytest.raises(OSError, match="synthetic directory sync failure"):
            derive.discard_after_restore(data, lock_path=tmp_path / "writer.lock")
    derive.discard_after_restore(data, lock_path=tmp_path / "writer.lock")
    assert calls == [data] * (failures + 1)


@pytest.mark.parametrize("data_exists", [False, True])
def test_restore_cache_discard_with_absent_cache(tmp_path, monkeypatch, data_exists):
    data = tmp_path / "data"
    if data_exists:
        data.mkdir()
    calls = []
    fsync = store._fsync_directory

    def record_sync(path):
        calls.append(path)
        fsync(path)

    monkeypatch.setattr(store, "_fsync_directory", record_sync)
    for _ in range(2):
        derive.discard_after_restore(data, lock_path=tmp_path / "writer.lock")
    assert calls == ([data, data] if data_exists else [])
    assert data.exists() == data_exists


@pytest.mark.parametrize("entry", [".staging", ".current.424242", "current", "."])
def test_rebuild_unlinks_cache_links_without_searching_their_targets(built, tmp_path, entry):
    _projects, data, _path = built
    root = data / "derived"
    target = root if entry == "." else root / entry
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    outside = tmp_path / "outside-cache"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    sentinel = nested / "retained"
    sentinel.write_text("keep")
    target.symlink_to(nested)
    outside.chmod(0)
    try:
        derive.build(data, if_stale=True)
    finally:
        outside.chmod(0o700)
    assert sentinel.read_text() == "keep"
    assert derive.is_current(data)
