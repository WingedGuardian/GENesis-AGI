"""Writer cutpoints retain the accepted selector and all required bodies."""

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import catalog, store
from genesis.transcript_analytics import publication as source_publication
from genesis.transcript_analytics.extract import TABLES


def _setup(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    source = projects / "a.jsonl"
    source.write_text('{"type":"progress"}\n')
    lock = tmp_path / "writer.lock"
    store.ingest(projects, data, lock_path=lock)
    return projects, data, source, lock


@pytest.mark.parametrize("table", TABLES)
def test_every_selected_table_schema_is_validated(tmp_path, table):
    projects, data, source, lock = _setup(tmp_path)
    selected = catalog.load(data)
    path = store._table_path(data, table, store.srckey(source.name), selected)
    metadata = pq.read_metadata(path).metadata
    pq.write_table(pa.table({"foreign": []}).replace_schema_metadata(metadata), path)
    keys, excluded = store.compatible_sources(data, selected)
    assert not keys and excluded[0]["reason"] == "incompatible table schema"


def test_initial_checkpoint_precedes_discovery_failure(tmp_path, monkeypatch):
    projects, data, source, lock = _setup(tmp_path)
    before = catalog.load(data)
    monkeypatch.setattr(
        store,
        "discover",
        lambda root: (_ for _ in ()).throw(OSError("synthetic discovery failure")),
    )
    with pytest.raises(OSError, match="discovery failure"):
        store.ingest(projects, data, lock_path=lock)
    after = catalog.load(data)
    assert after.sources == before.sources and after.revision != before.revision
    assert not after.collection["complete"] and not after.collection["discovery_complete"]
    assert after.collection["discovered"] is None and after.collection["unbuilt"] is None


@pytest.mark.parametrize("boundary", ["file_fsync", "replace", "directory_fsync"])
def test_selector_failure_is_fatal_before_extraction_or_cleanup(tmp_path, monkeypatch, boundary):
    projects, data, source, lock = _setup(tmp_path)
    before = catalog.load(data)
    originals = {
        store._table_path(data, table, store.srckey(source.name), before): store._table_path(
            data, table, store.srckey(source.name), before
        ).read_bytes()
        for table in TABLES
    }
    monkeypatch.setattr(
        store, "extract_source", lambda *a, **k: pytest.fail("must stop before extraction")
    )
    monkeypatch.setattr(
        source_publication,
        "collect",
        lambda *a: pytest.fail("must not clean after failed publication"),
    )
    if boundary == "file_fsync":
        original = os.fsync

        def failed(descriptor):
            if "/.staging/catalog." in os.readlink(f"/proc/self/fd/{descriptor}"):
                raise OSError("synthetic file fence failure")
            return original(descriptor)

        monkeypatch.setattr(os, "fsync", failed)
    elif boundary == "replace":
        monkeypatch.setattr(
            source_publication.os,
            "replace",
            lambda *a: (_ for _ in ()).throw(OSError("synthetic replace failure")),
        )
    else:
        original = source_publication.fsync_directory

        def failed(path):
            if path == data and catalog.load(data).revision != before.revision:
                raise OSError("synthetic directory fence failure")
            return original(path)

        monkeypatch.setattr(source_publication, "fsync_directory", failed)
    with pytest.raises(source_publication.Failed) as failure:
        store.ingest(projects, data, lock_path=lock)
    assert all(path.read_bytes() == body for path, body in originals.items())
    after = catalog.load(data)
    assert after.sources == before.sources
    if boundary == "directory_fsync":
        assert isinstance(failure.value, source_publication.Uncertain)
        assert failure.value.visible_revision == after.revision
        assert not after.collection["complete"]
    else:
        assert after.revision == before.revision


def test_staging_and_orphan_cleanup_requires_fence_and_preserves_unknown_paths(
    tmp_path, monkeypatch
):
    projects, data, source, lock = _setup(tmp_path)
    staging = data / ".staging/sources"
    valid = staging / ("e" * 32)
    valid.mkdir()
    (valid / "fragments.parquet").write_bytes(b"unfinished")
    unknown = staging / ("f" * 32)
    unknown.mkdir()
    (unknown / "foreign").write_bytes(b"retained")
    linked = staging / ("d" * 32)
    external = tmp_path / "external"
    external.mkdir()
    (external / "fragments.parquet").write_bytes(b"outside")
    linked.symlink_to(external, target_is_directory=True)
    original = source_publication.durable_selector
    monkeypatch.setattr(
        source_publication,
        "durable_selector",
        lambda *a: (_ for _ in ()).throw(OSError("synthetic fence")),
    )
    assert not source_publication.collect(data, TABLES)
    assert valid.is_dir() and unknown.is_dir() and linked.is_symlink()
    monkeypatch.setattr(source_publication, "durable_selector", original)
    assert source_publication.collect(data, TABLES)
    assert not valid.exists() and unknown.is_dir() and linked.is_symlink()
    assert (external / "fragments.parquet").read_bytes() == b"outside"


def test_retry_fences_existing_ancestors_after_failed_mkdir_entry_fence(tmp_path, monkeypatch):
    target = tmp_path / "one/two/three"
    original = source_publication.fsync_directory
    calls = []

    def failed(path):
        calls.append(path)
        if path == tmp_path / "one":
            raise OSError("synthetic ancestor entry failure")
        return original(path)

    monkeypatch.setattr(source_publication, "fsync_directory", failed)
    with pytest.raises(OSError, match="ancestor entry failure"):
        source_publication.make_directory(target)
    assert target.is_dir()
    calls.clear()
    monkeypatch.setattr(
        source_publication, "fsync_directory", lambda path: (calls.append(path), original(path))[1]
    )
    source_publication.make_directory(target)
    assert {tmp_path, tmp_path / "one", tmp_path / "one/two"}.issubset(calls)


@pytest.mark.parametrize(
    "boundary",
    ["staged_directory", "new_key_entry", "rename", "destination_parent", "staging_parent"],
)
def test_generation_finalization_failure_never_retires_old_selected_source(
    tmp_path, monkeypatch, boundary
):
    projects, data, source, lock = _setup(tmp_path)
    before = catalog.load(data)
    renamed = projects / "renamed.jsonl"
    source.rename(renamed)
    new_key = store.srckey(renamed.name)
    original_fence = source_publication.fsync_directory
    original_rename = os.rename
    finalized = False
    injected = False

    def rename(src, destination):
        nonlocal finalized
        if boundary == "rename" and destination.parent == data / "sources" / new_key:
            raise OSError("synthetic generation rename failure")
        result = original_rename(src, destination)
        if destination.parent == data / "sources" / new_key:
            finalized = True
        return result

    def fence(path):
        nonlocal injected
        fail = (
            boundary == "staged_directory"
            and path.parent == data / ".staging/sources"
            or boundary == "new_key_entry"
            and path == data / "sources"
            or boundary == "destination_parent"
            and finalized
            and path == data / "sources" / new_key
            or boundary == "staging_parent"
            and finalized
            and path == data / ".staging/sources"
        )
        if fail and not injected:
            injected = True
            raise OSError("synthetic generation directory fence failure")
        return original_fence(path)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(source_publication, "fsync_directory", fence)
    result = store.ingest(projects, data, lock_path=lock)
    assert result["failed"] == 1 and result["rebuilt_committed"] == 0
    assert catalog.load(data).sources == before.sources
    assert store.compatible_sources(data)[0] == list(before.sources)
    monkeypatch.setattr(os, "rename", original_rename)
    monkeypatch.setattr(source_publication, "fsync_directory", original_fence)
    assert store.ingest(projects, data, lock_path=lock)["rebuilt_committed"] == 1
    assert set(store.compatible_sources(data)[0]) == set(before.sources) | {new_key}


def test_persistent_orphan_cleanup_error_is_nonzero_and_retains_accepted_generation(
    tmp_path, monkeypatch
):
    projects, data, source, lock = _setup(tmp_path)
    before = catalog.load(data)
    old = store._table_path(data, "fragments", store.srckey(source.name), before)
    renamed = projects / "renamed.jsonl"
    source.rename(renamed)
    new_parent = data / "sources" / store.srckey(renamed.name)
    original = source_publication.fsync_directory

    def fail(path):
        # GC may already have removed the orphan before its directory fsync.
        if path == new_parent:
            raise OSError("persistent orphan parent failure")
        return original(path)

    monkeypatch.setattr(source_publication, "fsync_directory", fail)
    with pytest.raises(OSError, match="orphan parent failure"):
        store.ingest(projects, data, lock_path=lock)
    assert catalog.load(data).sources == before.sources
    assert not catalog.load(data).collection["complete"]
    assert old.is_file()


def test_checkpoint_count_time_and_first_progress_are_independent_of_population(
    tmp_path, monkeypatch
):
    projects, data, source, lock = _setup(tmp_path)
    publisher = store._Publisher(data, projects, False)
    observations = []
    monkeypatch.setattr(publisher, "checkpoint", lambda: observations.append(publisher.pending))
    publisher.pending = 1
    publisher.due()
    assert observations == [1] and not publisher.first_progress
    observations.clear()
    publisher.pending = 127
    publisher.last_checkpoint = store.time.monotonic()
    publisher.due()
    assert not observations
    publisher.pending = 128
    publisher.due()
    assert observations == [128]
    observations.clear()
    publisher.pending = 1
    publisher.last_checkpoint = store.time.monotonic() - 31
    publisher.due()
    assert observations == [1]


def test_invalid_selector_candidate_is_fatal_before_any_filesystem_mutation(tmp_path):
    with pytest.raises(source_publication.Failed, match="candidate failed validation"):
        source_publication.publish(tmp_path / "absent", "{}")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("uncertain", [False, True])
def test_temp_cleanup_error_cannot_mask_fatal_publication(tmp_path, monkeypatch, uncertain):
    projects, data, source, lock = _setup(tmp_path)
    selected = catalog.load(data)
    original_replace = os.replace
    original_fence = source_publication.fsync_directory
    original_unlink = type(data).unlink

    replaced = False

    def replace(src, dst):
        nonlocal replaced
        if not uncertain:
            raise OSError("replace fault")
        result = original_replace(src, dst)
        replaced = True
        return result

    def fence(path):
        if uncertain and replaced and path == data:
            raise OSError("selector directory fault")
        return original_fence(path)

    def unlink(path, *args, **kwargs):
        if path.name.startswith("catalog."):
            raise OSError("cleanup fault")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(source_publication, "fsync_directory", fence)
    monkeypatch.setattr(type(data), "unlink", unlink)
    expected = source_publication.Uncertain if uncertain else source_publication.Failed
    with pytest.raises(expected) as captured:
        source_publication.publish(data, __import__("json").dumps(selected.document()))
    assert "cleanup fault" not in str(captured.value)


def test_failed_checkpoint_reports_durable_prefix_separately(tmp_path, monkeypatch):
    projects, data, source, lock = _setup(tmp_path)
    publisher = store._Publisher(data, projects, False)
    durable_revision = publisher.selected.revision
    publisher.pending = 2
    publisher.rebuilt = 3
    publisher.committed_rebuilt = 1

    def fail(*args):
        raise source_publication.Uncertain("directory fault")

    monkeypatch.setattr(source_publication, "publish", fail)
    with pytest.raises(source_publication.Uncertain) as captured:
        publisher.checkpoint()
    failure = captured.value
    assert failure.staged_unpublished == 2
    assert failure.rebuilt_staged == 3
    assert failure.rebuilt_durably_committed == 1
    assert failure.last_durable_revision == durable_revision
    assert publisher.pending == 2
