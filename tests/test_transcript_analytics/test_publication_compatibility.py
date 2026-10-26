"""The audited storage-only bridge never widens genuine row compatibility."""

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import derive, query, store
from genesis.transcript_analytics.extract import TABLES


def _setup(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    source = projects / "source.jsonl"
    source.write_text('{"type":"progress"}\n')
    store.ingest(projects, data, lock_path=tmp_path / "writer.lock")
    return data, store.srckey(source.name)


@pytest.mark.parametrize("table", TABLES)
def test_individually_equivalent_semantics_cannot_form_mixed_six_table_commit(tmp_path, table):
    data, key = _setup(tmp_path)
    path = store._table_path(data, table, key)
    body = pq.read_table(path)
    metadata = dict(body.schema.metadata)
    # Both stamps have identical row semantics; one table is from a different commit.
    metadata[b"ta.generation"] = ("f" * 32 if metadata[b"ta.generation"] != b"f" * 32 else "e" * 32).encode()
    assert store.semantics_compatible(metadata)
    pq.write_table(body.replace_schema_metadata(metadata), path)
    keys, excluded = store.compatible_sources(data)
    assert not keys and excluded[0]["reason"] == "mixed generation"


@pytest.mark.parametrize(
    "name", ["extract.py", "classify.py", "schema.py", "scrub.py", "identity.py"]
)
def test_every_dependency_change_invalidates_exact_row_semantics(tmp_path, monkeypatch, name):
    baseline = Path(store.__file__).parent
    for dependency in ("extract.py", "classify.py", "schema.py", "scrub.py", "identity.py"):
        (tmp_path / dependency).write_bytes((baseline / dependency).read_bytes())
    monkeypatch.setattr(store, "__file__", str(tmp_path / "store.py"))
    assert store._extraction_signature() == store.EXTRACTION_VERSION
    stored = store.semantics()
    assert store.semantics_compatible(stored)
    with (tmp_path / name).open("ab") as handle:
        handle.write(b"\n# different dependency\n")
    monkeypatch.setattr(store, "EXTRACTION_VERSION", store._extraction_signature())
    assert not store.semantics_compatible(stored)


@pytest.mark.parametrize(
    "field,value",
    [(b"ta.extract", b"arbitrary"), (b"ta.schema", b"2"), (b"ta.scrub", b"untrusted")],
)
def test_arbitrary_or_mismatched_semantics_do_not_alias(field, value):
    stored = {**store.semantics(), b"ta.extract": store._BRIDGE_PREDECESSOR.encode(), field: value}
    assert not store.semantics_compatible(stored)


def test_retired_snapshot_signature_is_refused_under_changed_row_schema(tmp_path):
    data, key = _setup(tmp_path)
    derive.build(data, lock_path=tmp_path / "writer.lock")
    path = data / "derived/current/MANIFEST.json"
    manifest = json.loads(path.read_text())
    manifest["semantics"]["ta.extract"] = store._BRIDGE_PREDECESSOR
    manifest.pop("catalog_revision")
    path.write_text(json.dumps(manifest))
    assert not query.snapshot_compatible(data) and not query.derived_current(data)
    assert query.run_query(data, "SELECT count(*) FROM turns")[1] == [(0,)]
