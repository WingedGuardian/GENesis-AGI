"""Verification must compare the actual live reader with the captured cache."""

import json

import pyarrow.parquet as pq

from genesis.transcript_analytics import derive, store, verify


def test_verify_detects_schema_valid_cache_rows_that_differ_from_live(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    (projects / "source.jsonl").write_text(json.dumps({
        "type": "assistant", "uuid": "event", "sessionId": "session",
        "message": {"id": "message", "model": "fixture", "stop_reason": "end_turn",
                    "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}},
    }) + "\n")
    assert store.ingest(projects, data)["rebuilt"] == 1
    derive.build(data)
    assert verify.verify(data)["ok"] is True
    turns = (data / "derived/current").resolve() / "turns.parquet"
    body = pq.ParquetFile(turns).read()
    assert body.num_rows == 1
    pq.write_table(body.slice(0, 0), turns)
    result = verify.verify(data)
    assert result["ok"] is False
    assert result["views"]["turns"]["live"]["rows"] == 1
    assert result["views"]["turns"]["snapshot"]["rows"] == 0


def test_status_keeps_captured_source_metadata_when_collection_publishes(tmp_path, monkeypatch):
    import argparse

    from genesis.transcript_analytics import cli

    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    source = projects / "source.jsonl"
    source.write_text(json.dumps({
        "type": "assistant", "uuid": "event", "sessionId": "session",
        "message": {"id": "message", "model": "fixture", "stop_reason": "end_turn",
                    "content": [], "usage": {"input_tokens": 1, "output_tokens": 1}},
    }) + "\n")
    assert store.ingest(projects, data)["rebuilt"] == 1
    derive.build(data)
    original = cli._marker_totals

    def publish_after_marker_paths_are_captured(markers):
        with source.open("a") as stream:
            stream.write("malformed record\n")
        assert store.ingest(projects, data)["rebuilt"] == 1
        return original(markers)

    monkeypatch.setattr(cli, "_marker_totals", publish_after_marker_paths_are_captured)
    result = cli._status(argparse.Namespace(data=data, projects=projects))
    assert result["malformed_lines"] == 0
    assert result["snapshot_current"] is True
