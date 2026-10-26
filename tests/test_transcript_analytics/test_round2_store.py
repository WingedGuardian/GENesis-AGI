"""Round-two deduplication, source identity, retention and migration contracts."""

import json
import os

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import derive, query, store
from genesis.transcript_analytics import publication as source_publication


def _assistant(mid="m", **extra):
    return {
        "type": "assistant",
        "sessionId": "s",
        "timestamp": "2025-01-01T00:00:00Z",
        "message": {"id": mid, "model": "m", "content": []},
        **extra,
    }


def _setup(tmp_path, records):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    path = projects / "x.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    assert store.ingest(projects, data)["failed"] == 0
    return projects, data, path


@pytest.mark.parametrize("view", ["fragments", "turns", "hooks", "events"])
def test_uuid_and_fallback_namespaces_cannot_collide(tmp_path, view):
    if view == "fragments":
        records = [_assistant("a", uuid="2"), _assistant("b")]
    elif view == "turns":
        records = [
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"model": "<synthetic>"},
                **extra,
            }
            for extra in ({"uuid": "x.jsonl:2"}, {})
        ]
    elif view == "hooks":
        records = [
            {"type": "attachment", "attachment": {"type": "hook_success"}, **extra}
            for extra in ({"uuid": "x.jsonl:2"}, {})
        ]
    else:
        records = [
            {"type": "system", "subtype": "api_error", **extra}
            for extra in ({"uuid": "x.jsonl:2"}, {})
        ]
    _, data, _ = _setup(tmp_path, records)
    assert query.run_query(data, f"select count(*) from {view}", live=True)[1] == [(2,)]


@pytest.mark.parametrize(
    "tail",
    [
        {"type": "progress", "timestamp": "2027-01-01T00:00:00Z"},
        {"type": "progress"},
        {"type": "progress", "timestamp": "bad"},
        {"type": "progress", "timestamp": "2025-01-01T00:00:00"},
        {"type": "progress", "timestamp": "0001-01-01T00:00:00+01:00"},
        {"type": "progress", "timestamp": "9999-12-31T23:59:59-01:00"},
        "malformed",
        "torn",
    ],
)
def test_retention_accounts_for_skipped_and_unknown_raw_records(tmp_path, tail):
    projects, data, path = _setup(tmp_path, [_assistant()])
    with path.open("a") as handle:
        handle.write(
            "{bad\n"
            if tail == "malformed"
            else "{partial"
            if tail == "torn"
            else json.dumps(tail) + "\n"
        )
    store.ingest(projects, data)
    path.unlink()
    assert store.prune(data, before="2026-01-01", projects=projects) == 0


def test_rename_retains_accepted_uuidless_physical_rows(tmp_path):
    records = [
        _assistant(),
        {"type": "system", "subtype": "api_error", "timestamp": "2025-01-01T00:00:00Z"},
    ]
    projects, data, path = _setup(tmp_path, records)
    derive.build(data)
    path.rename(projects / "renamed.jsonl")
    assert store.ingest(projects, data)["failed"] == 0
    assert query.snapshot_compatible(data) and not query.derived_current(data)
    assert len(store.compatible_sources(data)[0]) == 2
    assert query.run_query(data, "select count(*) from raw_fragments", live=True)[1] == [(2,)]
    assert query.run_query(data, "select count(*) from events", live=True)[1] == [(2,)]


def test_failed_rename_staging_retains_original(tmp_path, monkeypatch):
    projects, data, path = _setup(tmp_path, [_assistant()])
    old = store.srckey(path.name)
    path.rename(projects / "renamed.jsonl")
    monkeypatch.setattr(
        pq, "write_table", lambda *a, **k: (_ for _ in ()).throw(OSError("stage failure"))
    )
    assert store.ingest(projects, data)["failed"] == 1
    assert store.compatible_sources(data)[0] == [old]


@pytest.mark.parametrize("fail_marker", [False, True])
def test_interrupted_rename_publication_retains_accepted_identities(
    tmp_path, monkeypatch, fail_marker
):
    projects, data, path = _setup(tmp_path, [_assistant()])
    derive.build(data)
    path.rename(projects / "renamed.jsonl")
    if fail_marker:
        original = source_publication.publish
        def fail(root, document):
            proposed = json.loads(document)
            if len(proposed["sources"]) == 2:
                raise source_publication.Failed("interrupted new-source selector publication")
            return original(root, document)

        monkeypatch.setattr(source_publication, "publish", fail)
        with pytest.raises(source_publication.Failed):
            store.ingest(projects, data)
        monkeypatch.setattr(source_publication, "publish", original)
    else:
        original = source_publication.finalize
        monkeypatch.setattr(
            source_publication,
            "finalize",
            lambda *a: (_ for _ in ()).throw(OSError("interrupted finalization")),
        )
        assert store.ingest(projects, data)["failed"] == 1
        monkeypatch.setattr(source_publication, "finalize", original)
    assert query.snapshot_compatible(data) and not query.derived_current(data)
    assert len(store.compatible_sources(data)[0]) == 1  # failed new source was never accepted
    assert store.srckey(path.name) in store.compatible_sources(data)[0]
    assert store.ingest(projects, data)["failed"] == 0
    assert len(store.compatible_sources(data)[0]) == 2
    assert not store.compatible_sources(data)[1]


def test_unmatched_disappeared_source_and_legacy_digest_are_retained(tmp_path):
    projects, data, path = _setup(tmp_path, [_assistant()])
    marker = store._table_path(data, "fragments", store.srckey(path.name))
    table = pq.read_table(marker)
    metadata = dict(table.schema.metadata)
    metadata.pop(b"ta.content_sha256")
    pq.write_table(table.replace_schema_metadata(metadata), marker)
    path.rename(projects / "renamed.jsonl")
    store.ingest(projects, data)
    # No content proof on old marker => no automatic retirement.
    assert len(store.compatible_sources(data)[0]) == 2
    (projects / "renamed.jsonl").unlink()
    (projects / "unrelated.jsonl").write_text(json.dumps(_assistant("other")) + "\n")
    store.ingest(projects, data)
    assert len(store.compatible_sources(data)[0]) == 3


def test_captured_digest_matches_rows_and_ignores_late_append(tmp_path, monkeypatch):
    import hashlib

    projects, data, path = _setup(tmp_path, [_assistant()])
    prefix = path.read_bytes()
    real = store.extract_source

    def append_after_extract(*args, **kwargs):
        result = real(*args, **kwargs)
        with path.open("a") as handle:
            handle.write(json.dumps(_assistant("late")) + "\n")
        return result

    monkeypatch.setattr(store, "extract_source", append_after_extract)
    store.build_source(path, path.name, data, path.stat())
    md, reason = store.source_metadata(data, store.srckey(path.name))
    assert reason is None
    assert md[b"ta.content_sha256"].decode() == hashlib.sha256(prefix).hexdigest()
    assert query.run_query(data, "select count(*) from raw_fragments", live=True)[1] == [(1,)]


def test_lossless_json_ids_survive_store_and_joins(tmp_path):
    from genesis.transcript_analytics.identity import decode_identity

    records = []
    for value in ("\ud800", "\ud801", "jsonid:eda080"):
        records.append(_assistant(value, uuid=value, sessionId=value))
        records[-1]["message"]["content"] = [
            {"type": "tool_use", "id": value, "name": "Bash", "input": {}}
        ]
        records.append(
            {
                "type": "user",
                "sessionId": value,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": value,
                            "is_error": False,
                            "content": "ok",
                        }
                    ]
                },
            }
        )
    _, data, _ = _setup(tmp_path, records)
    rows = query.run_query(
        data, "select session_id,tool_use_id,has_result from tool_calls", live=True
    )[1]
    assert len(rows) == 3 and all(r[2] for r in rows)
    assert {decode_identity(r[0]) for r in rows} == set(("\ud800", "\ud801", "jsonid:eda080"))
    assert {decode_identity(r[1]) for r in rows} == set(("\ud800", "\ud801", "jsonid:eda080"))


def test_present_hardlink_and_metadata_collision_do_not_retire_sources(tmp_path):
    projects, data, path = _setup(tmp_path, [_assistant()])
    second = projects / "hardlink.jsonl"
    os.link(path, second)
    store.ingest(projects, data)
    assert len(store.compatible_sources(data)[0]) == 2
    initial = path.stat()
    path.unlink()
    # Same inode/size/mtime but different bytes is not proof of a rename.
    payload = second.read_bytes().replace(b'"m"', b'"n"')
    second.write_bytes(payload)
    os.utime(second, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    store.ingest(projects, data)
    assert len(store.compatible_sources(data)[0]) == 2


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "1"])
def test_since_rejected_before_any_store_mutation(tmp_path, value):
    data = tmp_path / "data"
    with pytest.raises(ValueError, match="since_days"):
        store.ingest(tmp_path / "missing", data, since_days=value)
    assert not data.exists()


def test_semantic_stamp_includes_storage_scrub_and_identity_transforms():
    import hashlib
    from pathlib import Path

    digest = hashlib.sha256(b"ta-extraction-v1\0")
    for name in ("extract.py", "classify.py", "schema.py", "scrub.py", "identity.py"):
        raw = Path(store.__file__).with_name(name).read_bytes()
        digest.update(name.encode() + b"\0" + len(raw).to_bytes(8, "big") + raw)
    expected = digest.hexdigest()
    assert expected == store.EXTRACTION_VERSION
