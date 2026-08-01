"""Tests for the memory_class Qdrant re-sync core
(genesis.memory.memory_class_backfill).

The Qdrant layer is faked (scroll_points / set_payload_batch monkeypatched); a
real in-memory SQLite supplies the authoritative memory_metadata rows. Covers:
the fact-override repair (the actual regression), the missing-key cases keyed on
the EFFECTIVE read default, the no-metadata skip, the idempotent match skip,
dry-run, pagination, and the verify counter.
"""

from __future__ import annotations

import sqlite3

import pytest

from genesis.memory import memory_class_backfill as mcb


@pytest.fixture
def sqlite_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, memory_class TEXT)")
    db.executemany(
        "INSERT INTO memory_metadata (memory_id, memory_class) VALUES (?, ?)",
        [
            ("p_override", "fact"),  # stored fact; Qdrant drifted to reference
            ("p_rule", "rule"),  # stored rule; Qdrant payload has no key
            ("p_factdefault", "fact"),  # stored fact; Qdrant payload has no key
            ("p_match", "fact"),  # already agrees
        ],
    )
    db.commit()
    yield db
    db.close()


def _fake_scroll(pages_by_collection):
    def scroll(client, *, collection, limit=1000, offset=None):
        pages = pages_by_collection.get(collection, [[]])
        idx = int(offset) if offset is not None else 0
        points = pages[idx]
        next_offset = str(idx + 1) if idx + 1 < len(pages) else None
        return points, next_offset

    return scroll


def _capture_sets(monkeypatch):
    sets: list = []
    monkeypatch.setattr(
        mcb,
        "set_payload_batch",
        lambda client, *, collection, point_ids, payload: sets.append((point_ids, payload)),
    )
    return sets


def test_resync_repairs_only_true_divergences(sqlite_db, monkeypatch):
    pages = {
        "episodic_memory": [
            [
                # drifted: recall reads 'reference' (0.7x penalty) but stored fact
                {"id": "p_override", "payload": {"memory_class": "reference"}},
                # missing key: recall defaults to 'fact' but stored 'rule'
                {"id": "p_rule", "payload": {}},
                # missing key but stored 'fact' == read default -> NOT a write
                {"id": "p_factdefault", "payload": {}},
                # already correct
                {"id": "p_match", "payload": {"memory_class": "fact"}},
                # no metadata row -> nothing authoritative -> skip
                {"id": "p_orphan", "payload": {"memory_class": "reference"}},
            ]
        ],
        "knowledge_base": [[]],
    }
    monkeypatch.setattr(mcb, "scroll_points", _fake_scroll(pages))
    sets = _capture_sets(monkeypatch)

    totals = mcb.resync_memory_class(sqlite_db, object(), dry_run=False)
    assert totals == {"fact": 1, "rule": 1}  # p_override->fact, p_rule->rule

    repaired = {pid: payload["memory_class"] for ids, payload in sets for pid in ids}
    assert repaired == {"p_override": "fact", "p_rule": "rule"}
    # the read-default match, the exact match, and the orphan are all left alone
    assert "p_factdefault" not in repaired
    assert "p_match" not in repaired
    assert "p_orphan" not in repaired


def test_resync_dry_run_counts_but_writes_nothing(sqlite_db, monkeypatch):
    pages = {
        "episodic_memory": [[{"id": "p_override", "payload": {"memory_class": "reference"}}]],
        "knowledge_base": [[]],
    }
    monkeypatch.setattr(mcb, "scroll_points", _fake_scroll(pages))
    calls = []
    monkeypatch.setattr(mcb, "set_payload_batch", lambda *a, **k: calls.append(1))
    totals = mcb.resync_memory_class(sqlite_db, object(), dry_run=True)
    assert totals == {"fact": 1}
    assert calls == []


def test_resync_paginates(sqlite_db, monkeypatch):
    pages = {
        "episodic_memory": [
            [{"id": "p_override", "payload": {"memory_class": "reference"}}],
            [{"id": "p_rule", "payload": {}}],
        ],
        "knowledge_base": [[]],
    }
    monkeypatch.setattr(mcb, "scroll_points", _fake_scroll(pages))
    seen = _capture_sets(monkeypatch)
    mcb.resync_memory_class(sqlite_db, object(), dry_run=False)
    got = {pid for ids, _ in seen for pid in ids}
    assert got == {"p_override", "p_rule"}  # both pages processed


def test_count_diverged_memory_class(sqlite_db, monkeypatch):
    pages = {
        "episodic_memory": [
            [
                {"id": "p_override", "payload": {"memory_class": "reference"}},  # diverged
                {"id": "p_match", "payload": {"memory_class": "fact"}},  # ok
                {"id": "p_factdefault", "payload": {}},  # ok (default)
            ]
        ],
        "knowledge_base": [[{"id": "p_rule", "payload": {}}]],  # diverged (stored rule)
    }
    monkeypatch.setattr(mcb, "scroll_points", _fake_scroll(pages))
    assert mcb.count_diverged_memory_class(sqlite_db, object()) == 2
