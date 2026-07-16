"""Tests for scripts/wing_payload_resync.py — the one-shot Qdrant wing
payload re-sync.

Covers the pure planning/grouping logic (real-wing grouping, skip of
NULL/general metadata, missing-metadata accounting), the batched payload
write (payload shape + dry-run no-op), the metadata read, and the paginated
stale-point scroll.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Load the script as a module (it's not a package — use importlib).
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "wing_payload_resync.py"
_spec = importlib.util.spec_from_file_location("wing_payload_resync", _SCRIPT_PATH)
wr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wr)

from genesis.memory.taxonomy import classify_life_domain  # noqa: E402

# ── plan_groups ─────────────────────────────────────────────────────────


def test_plan_groups_real_wing_grouped_by_wing_room():
    stale = [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]
    meta = {
        "p1": ("routing", "fallback"),
        "p2": ("routing", "fallback"),
        "p3": ("memory", "recall"),
    }
    groups, skipped, missing = wr.plan_groups(stale, meta)
    assert groups[("routing", "fallback")] == ["p1", "p2"]
    assert groups[("memory", "recall")] == ["p3"]
    assert skipped == 0
    assert missing == 0


def test_plan_groups_skips_null_and_general_wing():
    stale = [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]
    meta = {
        "p1": (None, None),
        "p2": ("general", "uncategorized"),
        "p3": ("", None),
    }
    groups, skipped, missing = wr.plan_groups(stale, meta)
    assert groups == {}
    assert skipped == 3
    assert missing == 0


def test_plan_groups_counts_missing_metadata():
    stale = [{"id": "ghost"}, {"id": "p1"}]
    meta = {"p1": ("memory", None)}
    groups, skipped, missing = wr.plan_groups(stale, meta)
    assert groups[("memory", None)] == ["p1"]
    assert skipped == 0
    assert missing == 1


# ── apply_resync ────────────────────────────────────────────────────────


def test_apply_resync_writes_expected_payload():
    client = MagicMock()
    groups = {("routing", "fallback"): ["p1", "p2"]}
    written = wr.apply_resync(client, "episodic_memory", groups, apply=True)
    assert written == 2
    client.set_payload.assert_called_once()
    _, kwargs = client.set_payload.call_args
    assert kwargs["collection_name"] == "episodic_memory"
    assert sorted(kwargs["points"]) == ["p1", "p2"]
    assert kwargs["payload"] == {
        "wing": "routing",
        "room": "fallback",
        "life_domain": classify_life_domain("routing"),
    }


def test_apply_resync_omits_room_when_null():
    client = MagicMock()
    groups = {("memory", None): ["p1"]}
    wr.apply_resync(client, "episodic_memory", groups, apply=True)
    _, kwargs = client.set_payload.call_args
    assert "room" not in kwargs["payload"]
    assert kwargs["payload"]["wing"] == "memory"


def test_apply_resync_dry_run_no_write_but_counts():
    client = MagicMock()
    groups = {("routing", "fallback"): ["p1", "p2"], ("memory", None): ["p3"]}
    written = wr.apply_resync(client, "episodic_memory", groups, apply=False)
    assert written == 3
    client.set_payload.assert_not_called()


# ── fetch_metadata_wings ────────────────────────────────────────────────


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, wing TEXT, room TEXT)")
    conn.executemany(
        "INSERT INTO memory_metadata (memory_id, wing, room) VALUES (?, ?, ?)",
        [("p1", "routing", "fallback"), ("p2", "memory", None)],
    )
    conn.commit()
    return conn


def test_fetch_metadata_wings_returns_wing_room():
    conn = _mem_db()
    try:
        meta = wr.fetch_metadata_wings(conn, ["p1", "p2", "absent"])
        assert meta == {"p1": ("routing", "fallback"), "p2": ("memory", None)}
        assert "absent" not in meta
    finally:
        conn.close()


def test_fetch_metadata_wings_chunks(monkeypatch):
    # Force a tiny chunk so the IN-batching loop runs multiple times.
    monkeypatch.setattr(wr, "SQLITE_IN_CHUNK", 1)
    conn = _mem_db()
    try:
        meta = wr.fetch_metadata_wings(conn, ["p1", "p2"])
        assert set(meta) == {"p1", "p2"}
    finally:
        conn.close()


# ── scroll_stale_wing_ids ───────────────────────────────────────────────


def _pt(pid, wing=None, room=None):
    return SimpleNamespace(id=pid, payload={"wing": wing, "room": room})


def test_scroll_stale_wing_ids_paginates_and_extracts_payload():
    client = MagicMock()
    # Two pages, then exhausted (next offset None).
    client.scroll.side_effect = [
        ([_pt("p1", wing=None), _pt("p2", wing="general", room="x")], "cursor"),
        ([_pt("p3", wing=None)], None),
    ]
    out = wr.scroll_stale_wing_ids(client, "episodic_memory")
    assert [r["id"] for r in out] == ["p1", "p2", "p3"]
    assert out[1] == {"id": "p2", "pwing": "general", "proom": "x"}
    assert client.scroll.call_count == 2


def test_scroll_stale_wing_ids_respects_cap():
    client = MagicMock()
    client.scroll.side_effect = [
        ([_pt("p1"), _pt("p2"), _pt("p3")], "cursor"),
    ]
    out = wr.scroll_stale_wing_ids(client, "episodic_memory", cap=2)
    assert [r["id"] for r in out] == ["p1", "p2"]
    # Cap reached on first page → no second scroll call.
    assert client.scroll.call_count == 1
