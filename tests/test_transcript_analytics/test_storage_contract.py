"""A1 release controls run without importing query or reconciliation."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import history, locks, store
from genesis.transcript_analytics.extract import TABLES
from genesis.transcript_analytics.schema import SCHEMAS

FIXTURES = Path(__file__).with_name("fixtures")


def seed(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "source.jsonl"
    write(path, 1)
    return projects, data, path


def write(path, number):
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "event",
                "sessionId": "session",
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {
                    "id": "message",
                    "model": "fixture",
                    "stop_reason": "end_turn",
                    "content": [],
                    "usage": {"input_tokens": 1, "output_tokens": number},
                },
            }
        )
        + "\n"
    )


def test_selected_record_and_metadata_survive_multiple_publications(tmp_path):
    projects, data, path = seed(tmp_path)
    store.ingest(projects, data)
    selected, excluded = store.select_sources(data)
    assert not excluded and len(selected) == 1
    original = selected[0]
    metadata = dict(original.metadata)
    for number in (2, 3):
        write(path, number)
        assert store.ingest(projects, data)["rebuilt"] == 1
    physical = [
        child for child in (data / "sources" / original.key).iterdir()
        if child.is_dir() and not child.is_symlink()
    ]
    assert len(physical) == 3
    for table in TABLES:
        assert (
            pq.read_metadata(original.path(table)).metadata[b"ta.record"] == metadata[b"ta.record"]
        )
    assert original.metadata == metadata
    assert pq.ParquetFile(original.path("fragments")).read()["output_tokens"].to_pylist() == [1]
    path.unlink()
    assert store.prune(data, before="2026-01-01", projects=projects) == 1
    assert not original.directory.exists()


@pytest.mark.parametrize("label", ["flat-v1", "record-v5"])
def test_audited_history_preserves_original_stamps_flags_and_unknowns(tmp_path, label):
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / label, data, symlinks=True)
    selected, excluded = store.select_sources(data)
    assert not excluded and len(selected) == 1
    record = selected[0]
    assert record.adapter == label
    assert not store.semantics_compatible(record.metadata)
    assert not store.is_current(data, record.key, json.loads(record.metadata[b"ta.fp"]))
    for table in TABLES:
        original = pq.ParquetFile(record.path(table)).read()
        adapted = history.read_table(record.path(table), table, record.adapter)
        assert adapted.schema.equals(SCHEMAS[table], check_metadata=False)
        assert adapted.schema.metadata == original.schema.metadata
        for name in original.column_names:
            assert adapted[name].equals(original[name])
        for name in set(SCHEMAS[table].names) - set(original.column_names):
            assert adapted[name].null_count == adapted.num_rows
    calls = history.read_table(record.path("tool_calls"), "tool_calls", record.adapter)
    assert calls["is_error"].to_pylist() == [True]
    assert calls["scrub_failed"].to_pylist() == [True]
    assert history.unknown_facts(record.adapter)["unrecoverable"]
    if label == "flat-v1":
        assert calls["actor_id"].to_pylist() == [None]
        assert history.unknown_facts(label)["missing_columns"]["fragments"]


def _historical_catalog(data, selection, projects_root="/tmp"):
    (data / "source-catalog.json").write_text(json.dumps({
        "format": "ta-source-catalog-v1",
        "revision": "a" * 32,
        "population_revision": "b" * 32,
        "projects_root": projects_root,
        "sources": selection,
        "collection": {},
    }))


def test_audited_catalog_selects_record_v5_without_current(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "record-v5", data, symlinks=True)
    key = "654b8297f331da22"
    generation = "4562a1dd28154d94aa214d66bdeb2b6f"
    (data / "sources" / key / "current").unlink()
    _historical_catalog(data, {key: generation})
    selected, excluded = store.select_sources(data)
    assert not excluded and len(selected) == 1
    assert selected[0].adapter == "record-v5"
    assert selected[0].directory == data / "sources" / key / generation


@pytest.mark.parametrize("catalog", ["missing-key", "malformed"])
def test_invalid_catalog_never_falls_back_to_flat_or_glob(tmp_path, catalog):
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "flat-v1", data)
    if catalog == "malformed":
        (data / "source-catalog.json").write_text("{")
    else:
        _historical_catalog(data, {})
    selected, excluded = store.select_sources(data)
    assert not selected
    assert excluded[0]["reason"] == "invalid source selector"
    assert len(list(data.glob("*__*.parquet"))) == len(TABLES)


def test_unknown_historical_signature_is_excluded_not_current(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "record-v5", data, symlinks=True)
    record = store.select_sources(data)[0][0]
    for table in TABLES:
        body = pq.ParquetFile(record.path(table)).read()
        md = dict(body.schema.metadata)
        md[b"ta.extract"] = b"unknown-signature"
        pq.write_table(body.replace_schema_metadata(md), record.path(table))
    selected, excluded = store.select_sources(data)
    assert not selected
    assert excluded[0]["reason"] == "incompatible semantics"
    assert len(list(data.rglob("*.parquet"))) == 6


def test_source_growth_during_extraction_fails_closed(tmp_path, monkeypatch):
    projects, data, path = seed(tmp_path)
    original = store.extract_source

    def grow(path, rel, stop_at=None, **kwargs):
        result = original(path, rel, stop_at=stop_at, **kwargs)
        with path.open("a") as stream:
            stream.write("\n")
        return result

    monkeypatch.setattr(store, "extract_source", grow)
    assert store.ingest(projects, data)["failed"] == 1
    assert store.compatible_sources(data) == ([], [])
    monkeypatch.setattr(store, "extract_source", original)
    assert store.ingest(projects, data)["rebuilt"] == 1


def test_descriptor_not_replaced_path_is_fingerprinted(tmp_path, monkeypatch):
    projects, data, path = seed(tmp_path)
    initial = path.read_bytes()
    original = store.extract_source

    def replace_after_open(path, rel, stop_at=None, fh=None):
        assert fh is not None
        replacement = path.with_suffix(".replacement")
        write(replacement, 99)
        os.replace(replacement, path)
        return original(path, rel, stop_at=stop_at, fh=fh)

    monkeypatch.setattr(store, "extract_source", replace_after_open)
    assert store.ingest(projects, data)["rebuilt"] == 1
    record = store.select_sources(data)[0][0]
    assert record.metadata[b"ta.content_sha256"].decode() == hashlib.sha256(initial).hexdigest()
    assert pq.ParquetFile(record.path("fragments")).read()["output_tokens"].to_pylist() == [1]
    assert not store.is_current(data, record.key, store.source_fingerprint(path))


def test_all_public_writes_and_prunes_take_the_one_writer_lock(tmp_path):
    projects, data, path = seed(tmp_path)
    lock = tmp_path / "writer.lock"
    with locks.writer(lock):
        with pytest.raises(store.Busy):
            store.ingest(projects, data, lock_path=lock)
        with pytest.raises(store.Busy):
            store.build_source(path, path.name, data, path.stat(), lock_path=lock)
        with pytest.raises(store.Busy):
            store.prune(data, before="2026-01-01", projects=projects, lock_path=lock)


@pytest.mark.parametrize("initial", [False, True])
def test_sigkill_mid_ingest_preserves_accepted_records(tmp_path, initial):
    projects, data, path = seed(tmp_path)
    if initial:
        store.ingest(projects, data)
    write(path, 22)
    code = """
import os,signal,sys
from pathlib import Path
import pyarrow.parquet as pq
from genesis.transcript_analytics import store
original=pq.write_table
def kill(body,path,**kwargs):
    if str(path).endswith('agents.parquet'):
        os.kill(os.getpid(),signal.SIGKILL)
    return original(body,path,**kwargs)
pq.write_table=kill
store.ingest(Path(sys.argv[1]),Path(sys.argv[2]),lock_path=Path(sys.argv[3]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(projects), str(data), str(tmp_path / "writer.lock")],
        env=dict(os.environ, PYTHONPATH=str(Path(store.__file__).parents[2])),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    selected, excluded = store.select_sources(data)
    assert not excluded and len(selected) == int(initial)
    if initial:
        assert pq.ParquetFile(selected[0].path("fragments")).read()[
            "output_tokens"
        ].to_pylist() == [1]
    assert store.ingest(projects, data)["rebuilt"] == 1
    assert not (data / ".staging").exists()
    selected, excluded = store.select_sources(data)
    assert not excluded and len(selected) == 1
    assert pq.ParquetFile(selected[0].path("fragments")).read()["output_tokens"].to_pylist() == [22]
