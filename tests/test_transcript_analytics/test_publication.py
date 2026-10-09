"""Actual publication paths and crash-boundary controls."""

import json
import shutil

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import catalog, derive, query, store
from genesis.transcript_analytics import publication as source_publication
from genesis.transcript_analytics.extract import TABLES


def _source(projects, name="session.jsonl"):
    source = projects / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "session",
                "uuid": "event",
                "message": {"id": "message", "content": [{"type": "text", "text": "safe"}]},
            }
        )
        + "\n"
    )
    return source


def test_actual_ingest_selects_one_complete_immutable_generation(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    _source(projects)
    result = store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    selected = catalog.load(data)
    key = store.srckey("session.jsonl")
    assert result["rebuilt_committed"] == 1
    assert selected.collection["complete"]
    assert selected.sources[key] != "legacy"
    assert all(store._table_path(data, table, key, selected).is_file() for table in TABLES)
    assert store.compatible_sources(data, selected) == ([key], [])
    old_paths = {table: store._table_path(data, table, key, selected) for table in TABLES}
    again = store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    current = catalog.load(data)
    assert again["unchanged"] == 1 and again["rebuilt_committed"] == 0
    assert current.sources == selected.sources
    assert current.population_revision == selected.population_revision
    assert current.revision != selected.revision
    assert all(path.is_file() for path in old_paths.values())


def test_coverage_checkpoint_makes_snapshot_stale_but_readable(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    _source(projects)
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    derive.build(data, lock_path=lock)
    assert query.derived_current(data)
    before = catalog.load(data)
    store.ingest(projects, data, lock_path=lock)
    after = catalog.load(data)
    assert before.population_revision == after.population_revision
    assert not query.derived_current(data)
    assert query.snapshot_compatible(data)
    assert query.run_query(data, "SELECT count(*) FROM turns")[1] == [(1,)]


def test_unavailable_legacy_source_migrates_without_reextract_and_snapshot_survives(
    tmp_path, monkeypatch
):
    projects, data = tmp_path / "projects", tmp_path / "data"
    source = _source(projects)
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    selected = catalog.load(data)
    key = store.srckey("session.jsonl")
    for table in TABLES:
        body = pq.read_table(store._table_path(data, table, key, selected))
        metadata = dict(body.schema.metadata)
        metadata[b"ta.extract"] = store.EXTRACTION_VERSION.encode()
        pq.write_table(body.replace_schema_metadata(metadata), data / f"{table}__{key}.parquet")
    (data / catalog.FILENAME).unlink()
    shutil.rmtree(data / "sources")
    (data / "projects-root.json").write_text(json.dumps({"projects_root": str(projects.resolve())}))
    derive.build(data, lock_path=lock)
    source.unlink()
    monkeypatch.setattr(
        store, "extract_source", lambda *a, **k: pytest.fail("must not reextract retained history")
    )
    result = store.ingest(projects, data, lock_path=lock)
    current = catalog.load(data)
    assert result["migrated"] == 1 and result["rebuilt"] == 0
    assert current.sources[key] != "legacy"
    assert store.compatible_sources(data, current) == ([key], [])
    assert all(
        pq.read_metadata(store._table_path(data, t, key, current)).metadata[b"ta.extract"]
        == store.EXTRACTION_VERSION.encode()
        for t in TABLES
    )
    assert query.snapshot_compatible(data) and not query.derived_current(data)
    assert query.run_query(data, "SELECT count(*) FROM turns")[1] == [(1,)]


def test_failed_refresh_retains_last_accepted_generation(tmp_path, monkeypatch):
    projects, data = tmp_path / "projects", tmp_path / "data"
    source = _source(projects)
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    before = catalog.load(data)
    source.write_text(source.read_text() + "\n")
    monkeypatch.setattr(
        store,
        "extract_source",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("synthetic extraction failure")),
    )
    result = store.ingest(projects, data, lock_path=lock)
    after = catalog.load(data)
    assert result["failed"] == 1
    assert after.sources == before.sources
    assert not after.collection["complete"]
    assert after.collection["failed_sources"] == ("session.jsonl",)
    assert store.compatible_sources(data, after)[0] == [store.srckey("session.jsonl")]


def test_gc_fence_failure_retains_orphan_generation(tmp_path, monkeypatch):
    projects, data = tmp_path / "projects", tmp_path / "data"
    _source(projects)
    store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    selected = catalog.load(data)
    key = store.srckey("session.jsonl")
    source = data / "sources" / key / selected.sources[key]
    orphan = source.parent / ("f" * 32)
    shutil.copytree(source, orphan)
    monkeypatch.setattr(
        source_publication,
        "durable_selector",
        lambda data: (_ for _ in ()).throw(OSError("synthetic fence failure")),
    )
    assert not source_publication.collect(data, TABLES)
    assert source.is_dir() and orphan.is_dir()


def test_missing_selected_body_excludes_only_affected_source(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    _source(projects, "a.jsonl")
    _source(projects, "b.jsonl")
    store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    selected = catalog.load(data)
    store._table_path(data, "events", store.srckey("a.jsonl"), selected).unlink()
    keys, excluded = store.compatible_sources(data, selected)
    assert keys == [store.srckey("b.jsonl")]
    assert len(excluded) == 1 and excluded[0]["reason"] == "missing or unreadable table"
    assert query.run_query(data, "SELECT count(*) FROM raw_fragments", live=True)[1] == [(1,)]


@pytest.mark.parametrize("mutation", ["missing", "corrupt"])
def test_catalog_unavailable_never_falls_back_to_compatible_snapshot(tmp_path, mutation):
    projects, data = tmp_path / "projects", tmp_path / "data"
    _source(projects)
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    derive.build(data, lock_path=lock)
    if mutation == "missing":
        (data / catalog.FILENAME).unlink()
    else:
        (data / catalog.FILENAME).write_text("{}")
    with pytest.raises(catalog.Unavailable):
        query.run_query(data, "SELECT count(*) FROM turns")


def test_uncertain_selector_reports_visibility_and_restart_fence_retains_bodies(
    tmp_path, monkeypatch
):
    projects, data = tmp_path / "projects", tmp_path / "data"
    _source(projects)
    store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    selected = catalog.load(data)
    key = store.srckey("session.jsonl")
    old = data / "sources" / key / selected.sources[key]
    new = old.parent / ("e" * 32)
    shutil.copytree(old, new)
    document = selected.document()
    document["revision"] = "d" * 32
    document["population_revision"] = "c" * 32
    document["sources"][key] = new.name
    original = source_publication.fsync_directory

    def failed_fence(path):
        if path == data and catalog.load(data).revision == "d" * 32:
            raise OSError("synthetic selector fence failure")
        return original(path)

    monkeypatch.setattr(source_publication, "fsync_directory", failed_fence)
    with pytest.raises(source_publication.Uncertain) as failure:
        source_publication.publish(data, json.dumps(document))
    assert failure.value.visible_revision == "d" * 32
    assert old.is_dir() and new.is_dir()
    assert not source_publication.collect(data, TABLES)
    assert old.is_dir() and new.is_dir()
    monkeypatch.setattr(source_publication, "fsync_directory", original)
    assert source_publication.collect(data, TABLES)
    assert not old.exists() and new.is_dir()


@pytest.mark.parametrize("hardlink", [False, True])
def test_full_source_identity_collision_preserves_first_source_and_reports_unbuilt(
    tmp_path, monkeypatch, hardlink
):
    projects, data = tmp_path / "projects", tmp_path / "data"
    first = _source(projects, "a.jsonl")
    monkeypatch.setattr(store, "srckey", lambda rel: "a" * 16)
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    before = catalog.load(data)
    second = projects / "b.jsonl"
    if hardlink:
        second.hardlink_to(first)
    else:
        second.write_text(first.read_text())
    result = store.ingest(projects, data, lock_path=lock)
    after = catalog.load(data)
    assert result["failed"] == 1 and result["unchanged"] == 1
    assert after.sources == before.sources
    assert after.collection["failed_sources"] == after.collection["unbuilt"] == ("b.jsonl",)
    assert store.source_metadata(data, "a" * 16)[0][b"ta.source"] == b"a.jsonl"


def test_vanished_rename_key_collision_retains_accepted_identity(tmp_path, monkeypatch):
    projects, data = tmp_path / "projects", tmp_path / "data"
    first = _source(projects, "a.jsonl")
    monkeypatch.setattr(store, "srckey", lambda rel: "a" * 16)
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    before = catalog.load(data)
    first.rename(projects / "b.jsonl")
    result = store.ingest(projects, data, lock_path=lock)
    after = catalog.load(data)
    assert result["failed"] == 1 and result["rebuilt_committed"] == 0
    assert after.sources == before.sources
    assert after.collection["failed_sources"] == after.collection["unbuilt"] == ("b.jsonl",)
    assert not after.collection["complete"]
    assert store.source_metadata(data, "a" * 16)[0][b"ta.source"] == b"a.jsonl"


def test_standalone_nested_source_binds_correct_projects_root(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "new-parent/data"
    source = _source(projects, "nested/session.jsonl")
    store.build_source(source, "nested/session.jsonl", data, source.stat())
    selected = catalog.load(data)
    assert selected.projects_root == str(projects.resolve())
    assert not selected.collection["complete"]
    assert store.compatible_sources(data)[0] == [store.srckey("nested/session.jsonl")]


def test_unrecognized_legacy_filenames_are_retained_without_catalog_authority(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    foreign = data / "fragments__foreign.parquet"
    foreign.write_bytes(b"not a selected source")
    assert store.source_keys(data) == []
    projects = tmp_path / "projects"
    projects.mkdir()
    result = store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    assert result["failed"] == 0
    assert not catalog.load(data).sources
    assert foreign.read_bytes() == b"not a selected source"
