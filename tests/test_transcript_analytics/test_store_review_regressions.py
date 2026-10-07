"""Retention and query checks against independently specified examples."""

import json
import os

import pytest

from genesis.transcript_analytics import derive, query, store


def setup_store(tmp_path, timestamps):
    root, data = tmp_path / "projects", tmp_path / "data"
    root.mkdir()
    for index, timestamp in enumerate(timestamps):
        record = {
            "type": "assistant",
            "sessionId": "s",
            "timestamp": timestamp,
            "message": {
                "id": str(index),
                "model": "m",
                "stop_reason": "end_turn",
                "usage": {"output_tokens": 2},
                "content": [],
            },
        }
        (root / f"{index}.jsonl").write_text(json.dumps(record) + "\n")
    store.ingest(root, data)
    return root, data


def test_prune_wrong_nonempty_root_refused(tmp_path):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    (root / "0.jsonl").unlink()
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    (wrong / "x.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="root differs"):
        store.prune(data, before="2026-01-01", projects=wrong)
    assert len(list(data.glob("fragments__*.parquet"))) == 1


def test_prune_utc_boundary_and_unknown_timestamps(tmp_path):
    root, data = setup_store(tmp_path, ["2025-12-31T19:30:00-05:00", "bad", "2025-01-01T00:00:00Z"])
    for path in root.glob("*.jsonl"):
        path.unlink()
    assert store.prune(data, before="2026-01-01", projects=root) == 1
    assert len(list(data.glob("fragments__*.parquet"))) == 2


def test_last_source_pruned_invalidates_and_builds_empty_snapshot(tmp_path):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    derive.build(data)
    (root / "0.jsonl").unlink()
    assert store.prune(data, before="2026-01-01", projects=root) == 1
    assert not query.snapshot_compatible(data)
    assert query.run_query(data, "select count(*) from turns")[1] == [(0,)]
    derive.build(data)
    assert query.snapshot_compatible(data)
    assert query.run_query(data, "select count(*) from turns")[1] == [(0,)]


def test_uuidless_and_api_error_turns_retained(tmp_path):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    api = {
        "type": "assistant",
        "sessionId": "s",
        "isApiErrorMessage": True,
        "message": {"model": "<synthetic>", "content": []},
    }
    (root / "errors.jsonl").write_text(json.dumps(api) + "\n" + json.dumps(api) + "\n")
    store.ingest(root, data)
    rows = query.run_query(
        data, "select message_id,n_records,output_tokens from turns order by message_id nulls last"
    )[1]
    assert rows == [("0", 1, 2), (None, 1, None), (None, 1, None)]


def test_filesystem_byte_path_roundtrip(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    relative = os.fsdecode(b"bad-\xff.jsonl")
    (root / relative).write_text("{}\n")
    data = tmp_path / "data"
    assert store.ingest(root, data)["failed"] == 0
    md, error = store.source_metadata(data, store.srckey(relative))
    assert error is None and os.fsdecode(md[b"ta.source"]) == relative
    assert store.source_path(store.source_identity(relative)) == relative


def test_raw_views_require_explicit_live_mode(tmp_path):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    derive.build(data)
    with pytest.raises(ValueError, match="require --live"):
        query.run_query(data, "select count(*) from raw_fragments")
    assert query.run_query(data, "select count(*) from raw_fragments", live=True)[1] == [(1,)]


def test_resumed_api_error_uuid_counts_once(tmp_path):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    record = {
        "type": "assistant",
        "uuid": "api-uuid",
        "sessionId": "s",
        "isApiErrorMessage": True,
        "message": {"model": "<synthetic>", "content": []},
    }
    for filename in ("a.jsonl", "b.jsonl"):
        (root / filename).write_text(json.dumps(record) + "\n")
    store.ingest(root, data)
    assert query.run_query(data, "select count(*),sum(n_records) from turns where is_api_error")[
        1
    ] == [(1, 1)]


def test_incompatible_byte_path_reports_exclusion(tmp_path, monkeypatch):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    relative = os.fsdecode(b"bad-\xff.jsonl")
    (root / relative).write_text("{}\n")
    store.ingest(root, data)
    monkeypatch.setattr(store, "SCHEMA_VERSION", "future")
    included, excluded = store.compatible_sources(data)
    assert not included
    assert store.source_identity(relative) in [entry["source"] for entry in excluded]


def test_repository_qualified_pr_links_survive_session_rollup(tmp_path):
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    records = [
        {"type": "pr-link", "sessionId": "s", "prNumber": 42, "prRepository": repo}
        for repo in ("org/one", "org/two")
    ]
    (root / "meta.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    store.ingest(root, data)
    links = query.run_query(data, "select pr_links from sessions")[1][0][0]
    assert {link["repository"] for link in links} == {"org/one", "org/two"}


def test_scrub_failure_survives_turns_and_session_rollup(tmp_path, monkeypatch):
    from genesis.transcript_analytics import extract

    monkeypatch.setattr(extract, "scrub_text", lambda value: (None, bool(value)))
    root, data = setup_store(tmp_path, ["2025-01-01T00:00:00Z"])
    assert query.run_query(data, "select scrub_failed from turns")[1] == [(True,)]
    assert query.run_query(data, "select scrub_failed from sessions")[1] == [(True,)]
