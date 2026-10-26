"""Independent-review boundary regressions using real Parquet bodies."""

import json
import shutil

import pytest

from genesis.transcript_analytics import catalog, store
from genesis.transcript_analytics import publication as publishing


def source(projects, name):
    path = projects / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "assistant", "uuid": name,
                               "message": {"id": name, "content": []}}) + "\n")
    return path


def test_standalone_builder_requires_writer_lease(tmp_path, monkeypatch):
    path = source(tmp_path / "projects", "a.jsonl")
    lock = tmp_path / "writer.lock"
    monkeypatch.setattr(store, "DEFAULT_LOCK", lock)
    with store._locked(lock), pytest.raises(store.Busy):
        store.build_source(path, "a.jsonl", tmp_path / "data", path.stat())


@pytest.mark.parametrize("damaged", store.TABLES)
def test_damaged_selected_body_cannot_hide_collision(tmp_path, monkeypatch, damaged):
    projects, data = tmp_path / "projects", tmp_path / "data"
    original = source(projects, "a.jsonl")
    store.build_source(original, "a.jsonl", data, original.stat())
    selected = catalog.load(data)
    key = store.srckey("a.jsonl")
    store._table_path(data, damaged, key, selected).unlink()
    other = source(projects, "b.jsonl")
    monkeypatch.setattr(store, "srckey", lambda _: key)
    with pytest.raises(store.SourceIdentityConflict):
        store.build_source(other, "b.jsonl", data, other.stat())
    assert catalog.load(data).sources == selected.sources


@pytest.mark.parametrize("failure", [publishing.Failed, KeyboardInterrupt])
def test_staging_retains_primary_failure_for_fenced_cleanup(tmp_path, monkeypatch, failure):
    path = source(tmp_path / "projects", "a.jsonl")
    monkeypatch.setattr(publishing, "finalize", lambda *args: (_ for _ in ()).throw(failure("primary")))
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cleanup")))
    with pytest.raises(failure, match="primary"):
        store.build_source(path, "a.jsonl", tmp_path / "data", path.stat())
    assert list((tmp_path / "data" / ".staging" / "sources").iterdir())
