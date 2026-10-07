"""Compatibility is semantic and commits cover all six tables."""

import json

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import store


@pytest.fixture
def source(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    path = projects / "agent-example.jsonl"
    path.write_text('{"type":"progress","sessionId":"example"}\n')
    return projects, path, tmp_path / "data"


def test_metadata_add_change_delete_rebuilds(source):
    projects, path, data = source
    assert store.ingest(projects, data)["rebuilt"] == 1
    meta = path.with_suffix(".meta.json")
    for value in ("reviewer", "builder", None):
        if value is None:
            meta.unlink()
        else:
            meta.write_text(json.dumps({"agentType": value}))
        assert store.ingest(projects, data)["rebuilt"] == 1
        rows = pq.read_table(data / f"agents__{store.srckey(path.name)}.parquet").to_pylist()
        assert [r["agent_type"] for r in rows] == ([] if value is None else [value])
        assert store.ingest(projects, data)["unchanged"] == 1


def test_mixed_generation_is_excluded_even_after_source_disappears(source):
    projects, path, data = source
    store.ingest(projects, data)
    table = data / f"hooks__{store.srckey(path.name)}.parquet"
    content = pq.read_table(table)
    pq.write_table(
        content.replace_schema_metadata({**content.schema.metadata, b"ta.generation": b"other"}),
        table,
    )
    path.unlink()
    accepted, excluded = store.compatible_sources(data)
    assert accepted == []
    assert excluded[0]["reason"] == "mixed generation"


def test_semantics_upgrade_retains_but_excludes_history(source, monkeypatch):
    projects, path, data = source
    store.ingest(projects, data)
    path.unlink()
    monkeypatch.setattr(store, "EXTRACTION_VERSION", "next")
    accepted, excluded = store.compatible_sources(data)
    assert accepted == []
    assert excluded[0]["reason"] == "incompatible semantics"
    assert len(list(data.glob("*.parquet"))) == 6


def test_symlink_sources_are_not_ingested(source):
    projects, path, data = source
    (projects / "linked.jsonl").symlink_to(path)
    assert store.ingest(projects, data)["sources"] == 1


def test_inventory_keeps_failed_never_ingested_source_visible(source, monkeypatch):
    projects, path, data = source

    def fail(*args):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(store, "build_source", fail)
    assert store.ingest(projects, data)["failed"] == 1
    inventory = store.inventory(data)
    assert inventory["discovered"] == 1
    assert inventory["unbuilt"] == inventory["failed_sources"] == [path.name]


def test_schema_upgrade_rebuilds_present_source(source, monkeypatch):
    projects, path, data = source
    store.ingest(projects, data)
    monkeypatch.setattr(store, "EXTRACTION_VERSION", "next")
    assert store.ingest(projects, data)["rebuilt"] == 1
    assert store.compatible_sources(data)[1] == []
