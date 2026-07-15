"""Tests for the origin_class backfill core (genesis.memory.origin_class_backfill).

The Qdrant layer is faked (scroll_points / set_payload_batch monkeypatched); a
real in-memory SQLite supplies the authoritative memory_metadata rows. Covers:
SQLite-authoritative mapping, payload-fallback for orphan points, the
idempotent skip of already-classified points, dry-run, and the verify counter.
"""

from __future__ import annotations

import sqlite3

import pytest

from genesis.memory import origin_class_backfill as ocb


@pytest.fixture
def sqlite_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, origin_class TEXT)")
    db.executemany(
        "INSERT INTO memory_metadata (memory_id, origin_class) VALUES (?, ?)",
        [("p1", "owner"), ("p2", "first_party")],
    )
    db.commit()
    yield db
    db.close()


def _fake_scroll(pages_by_collection):
    """Return a scroll_points stand-in that paginates the given points."""

    def scroll(client, *, collection, limit=1000, offset=None):
        pages = pages_by_collection.get(collection, [[]])
        idx = int(offset) if offset is not None else 0
        points = pages[idx]
        next_offset = str(idx + 1) if idx + 1 < len(pages) else None
        return points, next_offset

    return scroll


def test_backfill_maps_sqlite_and_skips_classified(sqlite_db, monkeypatch):
    # p1/p2 missing origin_class in Qdrant; p3 already has it (must be skipped).
    pages = {
        "episodic_memory": [
            [
                {"id": "p1", "payload": {}},
                {"id": "p2", "payload": {}},
                {"id": "p3", "payload": {"origin_class": "owner"}},
            ]
        ],
        "knowledge_base": [[]],
    }
    monkeypatch.setattr(ocb, "scroll_points", _fake_scroll(pages))
    sets: list = []
    monkeypatch.setattr(
        ocb,
        "set_payload_batch",
        lambda client, *, collection, point_ids, payload: sets.append((point_ids, payload)),
    )

    totals = ocb.backfill_origin_class(sqlite_db, object(), dry_run=False)
    assert totals == {"owner": 1, "first_party": 1}
    # p3 (already classified) never appears in a set call.
    set_ids = {pid for ids, _ in sets for pid in ids}
    assert set_ids == {"p1", "p2"}


def test_backfill_orphan_point_uses_payload_fallback(sqlite_db, monkeypatch):
    # p9 has no memory_metadata row -> origin is DERIVED from payload, not skipped.
    pages = {
        "episodic_memory": [[{"id": "p9", "payload": {"source_subsystem": "reflection"}}]],
        "knowledge_base": [[]],
    }
    monkeypatch.setattr(ocb, "scroll_points", _fake_scroll(pages))
    captured: list = []
    monkeypatch.setattr(
        ocb,
        "set_payload_batch",
        lambda client, *, collection, point_ids, payload: captured.append((point_ids, payload)),
    )
    totals = ocb.backfill_origin_class(sqlite_db, object(), dry_run=False)
    assert sum(totals.values()) == 1  # p9 classified via fallback
    assert captured and captured[0][0] == ["p9"]
    assert captured[0][1]["origin_class"]  # some non-empty derived class


def test_backfill_dry_run_writes_nothing(sqlite_db, monkeypatch):
    pages = {"episodic_memory": [[{"id": "p1", "payload": {}}]], "knowledge_base": [[]]}
    monkeypatch.setattr(ocb, "scroll_points", _fake_scroll(pages))
    calls = []
    monkeypatch.setattr(ocb, "set_payload_batch", lambda *a, **k: calls.append(1))
    totals = ocb.backfill_origin_class(sqlite_db, object(), dry_run=True)
    assert totals == {"owner": 1}  # counted...
    assert calls == []  # ...but not written


def test_backfill_paginates(sqlite_db, monkeypatch):
    pages = {
        "episodic_memory": [
            [{"id": "p1", "payload": {}}],
            [{"id": "p2", "payload": {}}],
        ],
        "knowledge_base": [[]],
    }
    monkeypatch.setattr(ocb, "scroll_points", _fake_scroll(pages))
    seen: list = []
    monkeypatch.setattr(
        ocb,
        "set_payload_batch",
        lambda client, *, collection, point_ids, payload: seen.extend(point_ids),
    )
    ocb.backfill_origin_class(sqlite_db, object(), dry_run=False)
    assert set(seen) == {"p1", "p2"}  # both pages processed


def test_count_missing_origin_class(monkeypatch):
    pages = {
        "episodic_memory": [
            [{"id": "p1", "payload": {}}, {"id": "p2", "payload": {"origin_class": "owner"}}]
        ],
        "knowledge_base": [[{"id": "p3", "payload": {}}]],
    }
    monkeypatch.setattr(ocb, "scroll_points", _fake_scroll(pages))
    assert ocb.count_missing_origin_class(object()) == 2  # p1 + p3 (p2 classified)
