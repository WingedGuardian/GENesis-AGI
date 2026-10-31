"""Real-reader coherence and historical live-view release controls."""

import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from genesis.transcript_analytics import query, store
from genesis.transcript_analytics.extract import TABLES


def _write(path, number):
    call = {
        "type": "assistant", "uuid": "call", "sessionId": "session",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "id": "message", "model": "fixture", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "tool", "name": "Bash",
                         "input": {"command": f"echo revision-{number}"}}],
            "usage": {"input_tokens": 1, "output_tokens": number},
        },
    }
    path.write_text(json.dumps(call) + "\n")


@pytest.mark.parametrize("pause_point", ["between_tables", "after_connection"])
def test_real_lazy_reader_survives_two_newer_publications(tmp_path, monkeypatch, pause_point):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "source.jsonl"
    _write(path, 1)
    assert store.ingest(projects, data)["rebuilt"] == 1
    initial = store.select_sources(data)[0][0]
    paused, resume = threading.Event(), threading.Event()
    selected, reader_thread = {}, []
    original = store.SourceRecord.path

    def table_path(record, table):
        binding = (
            reader_thread and threading.get_ident() == reader_thread[0]
            and sys._getframe(1).f_code is query._install_live.__code__
        )
        if binding and pause_point == "between_tables" and table == TABLES[1]:
            paused.set()
            assert resume.wait(30), "publisher did not release reader"
        result = original(record, table)
        if binding:
            selected[table] = result
        return result

    monkeypatch.setattr(store.SourceRecord, "path", table_path)

    def read():
        reader_thread.append(threading.get_ident())
        with query.connect(data, live=True) as con:
            captured = query._READS[con]["inputs_signature"]
            if pause_point == "after_connection":
                paused.set()
                assert resume.wait(30), "publisher did not release reader"
            rows = con.execute(
                "select t.command,f.output_tokens from raw_tool_calls t "
                "cross join raw_fragments f"
            ).fetchall()
            assert query._READS[con]["inputs_signature"] == captured
            return rows

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read)
        assert paused.wait(30), "reader did not reach deterministic pause"
        try:
            for number in (2, 3):
                _write(path, number)
                assert store.ingest(projects, data)["rebuilt"] == 1
        finally:
            resume.set()
        assert future.result(timeout=30) == [("echo revision-1", 1)]
    assert set(selected) == set(TABLES)
    assert {path.parent for path in selected.values()} == {initial.directory}
    assert all(path.is_file() for path in selected.values())
    with query.connect(data) as con:
        assert con.execute("select command from tool_calls").fetchall() == [("echo revision-3",)]


@pytest.mark.parametrize("label", ["flat-v1", "record-v5"])
def test_genuine_historical_rows_remain_usable_without_originals(tmp_path, label):
    data = tmp_path / "data"
    shutil.copytree(Path(__file__).with_name("fixtures") / label, data, symlinks=True)
    assert not list(tmp_path.rglob("*.jsonl"))
    record = store.select_sources(data)[0][0]
    with query.connect(data, live=True) as con:
        assert con.execute(
            "select command,is_error,scrub_failed from tool_calls"
        ).fetchall() == [("echo historical", True, True)]
        origin, unknown = con.execute(
            "select provenance,unknown_facts from source_coverage"
        ).fetchone()
        assert json.loads(origin)["ta.extract"] == record.metadata[b"ta.extract"].decode()
        assert json.loads(origin)["ta.scrub"] == record.metadata[b"ta.scrub"].decode()
        assert json.loads(unknown)["unrecoverable"]
        if label == "flat-v1":
            assert con.execute(
                "select executor_id,attribution_status from tool_calls"
            ).fetchall() == [(None, "unresolved")]
            assert con.execute("select actor_id from raw_fragments").fetchall() == [(None,)]
            assert con.execute("select scrub_failed from raw_fragments").fetchall() == [(None,)]
    manifest = tmp_path / "query.json"
    assert query.run_query(
        data, "select count(*) from tool_calls", manifest_path=manifest, live=True
    )[1] == [(1,)]
    coverage = json.loads(manifest.read_text())["coverage"]
    assert coverage["included"] == 1 and not coverage["excluded"]
    assert coverage["historical"][0]["adapter"] == label


def test_query_metadata_does_not_reresolve_source_selection(tmp_path, monkeypatch):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    _write(projects / "source.jsonl", 1)
    store.ingest(projects, data)

    def unexpected(*args, **kwargs):
        raise AssertionError("query evidence must use captured source metadata")

    monkeypatch.setattr(store, "source_metadata", unexpected)
    manifest = tmp_path / "query.json"
    assert query.run_query(
        data, "select count(*) from tool_calls", live=True, manifest_path=manifest
    )[1] == [(1,)]
    assert json.loads(manifest.read_text())["source_references"] == [store.srckey("source.jsonl")]
