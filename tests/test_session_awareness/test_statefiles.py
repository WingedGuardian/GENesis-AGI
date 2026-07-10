"""Statefile tests — tmp_path isolation, explicit clock, no wall time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.session_awareness.statefiles import (
    empty_state,
    load_state,
    save_state,
    theme_path,
)

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def test_roundtrip(tmp_path):
    s = empty_state("sess-1")
    s["ema"] = [0.5, 0.5]
    s["ema_turns"] = 2
    s["updated_at"] = NOW.isoformat()
    save_state("sess-1", s, base=tmp_path)
    loaded = load_state("sess-1", base=tmp_path, now=NOW)
    assert loaded == s


def test_traversal_guard():
    assert theme_path("") is None
    assert theme_path("../evil") is None
    assert theme_path("a/b") is None
    assert theme_path("..") is None


def test_traversal_guard_noops(tmp_path):
    save_state("../evil", {"x": 1}, base=tmp_path)
    assert list(tmp_path.iterdir()) == []
    loaded = load_state("../evil", base=tmp_path, now=NOW)
    assert loaded == empty_state("../evil")


def test_missing_file_returns_empty(tmp_path):
    assert load_state("nope", base=tmp_path, now=NOW) == empty_state("nope")


def test_corrupt_file_returns_empty(tmp_path):
    d = tmp_path / "sess-c"
    d.mkdir()
    (d / "session_theme.json").write_text("{not json")
    assert load_state("sess-c", base=tmp_path, now=NOW) == empty_state("sess-c")
    (d / "session_theme.json").write_text('{"ring": "wrong-type"}')
    assert load_state("sess-c", base=tmp_path, now=NOW) == empty_state("sess-c")


def test_stale_state_softens_not_resets(tmp_path):
    """A multi-day session keeps its identity (EMA + decayed ledger) but
    re-arms the trigger: ring/fired/claims cleared (user decision,
    2026-07-09 — one CC session spans days and many compactions)."""
    s = empty_state("sess-s")
    s["ema"] = [1.0]
    s["ema_turns"] = 5
    s["entities"] = {"voice": 2.0, "faint": 0.08}
    s["ring"] = [[1.0]] * 3
    s["fired"] = [{"ema": [1.0], "turn": 4, "at": "x"}]
    s["fired_count"] = 3
    s["worker_pending_since"] = "2026-07-08T00:00:00+00:00"
    s["updated_at"] = (NOW - timedelta(hours=25)).isoformat()
    save_state("sess-s", s, base=tmp_path)
    loaded = load_state("sess-s", base=tmp_path, now=NOW)
    assert loaded["ema"] == [1.0]  # theme identity survives
    assert loaded["ema_turns"] == 5
    assert loaded["entities"] == {"voice": 1.0}  # halved; faint pruned
    assert loaded["ring"] == []
    assert loaded["fired"] == []
    assert loaded["fired_count"] == 0
    assert loaded["worker_pending_since"] is None
    # Under the threshold: fully preserved
    s["updated_at"] = (NOW - timedelta(hours=23)).isoformat()
    save_state("sess-s", s, base=tmp_path)
    loaded = load_state("sess-s", base=tmp_path, now=NOW)
    assert loaded["fired_count"] == 3
    assert loaded["ring"] != []


def test_missing_keys_backfilled(tmp_path):
    d = tmp_path / "sess-m"
    d.mkdir()
    (d / "session_theme.json").write_text(
        '{"ring": [], "entities": {}, "fired": []}'
    )
    loaded = load_state("sess-m", base=tmp_path, now=NOW)
    assert loaded["ema"] is None
    assert loaded["outlier_skips"] == 0
    assert loaded["session_id"] == "sess-m"


def test_atomic_write_leaves_no_tmp(tmp_path):
    save_state("sess-a", empty_state("sess-a"), base=tmp_path)
    leftovers = [p for p in (tmp_path / "sess-a").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
