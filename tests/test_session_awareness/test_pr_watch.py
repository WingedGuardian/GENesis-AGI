"""Tests for the PR-watch inline-surface core (pure logic + DB read + sidecar)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.session_awareness import pr_watch

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _make_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE outreach_history ("
        "id TEXT PRIMARY KEY, topic TEXT, category TEXT, delivered_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO outreach_history (id, topic, category, delivered_at) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# read_steward_notifications
# --------------------------------------------------------------------------


def test_read_filters_category_topic_and_lookback(tmp_path):
    db = tmp_path / "g.db"
    _make_db(
        db,
        [
            (
                "a",
                "PR steward tick: litellm#27447 mergeable",
                "notification",
                (NOW - timedelta(days=1)).isoformat(),
            ),
            (
                "b",
                "Upstream PR Steward baseline",
                "notification",
                (NOW - timedelta(days=90)).isoformat(),
            ),  # outside lookback
            (
                "c",
                "Billy brain is live",
                "notification",
                (NOW - timedelta(days=1)).isoformat(),
            ),  # not steward
            (
                "d",
                "PR steward tick: x",
                "digest",
                (NOW - timedelta(days=1)).isoformat(),
            ),  # wrong category
        ],
    )
    out = pr_watch.read_steward_notifications(db, lookback_days=30, now=NOW)
    assert [r["id"] for r in out] == ["a"]


def test_read_case_insensitive_steward_match(tmp_path):
    db = tmp_path / "g.db"
    _make_db(
        db,
        [
            (
                "a",
                "Upstream PR Steward is live",
                "notification",
                (NOW - timedelta(days=2)).isoformat(),
            )
        ],
    )
    out = pr_watch.read_steward_notifications(db, lookback_days=30, now=NOW)
    assert len(out) == 1


def test_read_missing_db_returns_empty(tmp_path):
    assert pr_watch.read_steward_notifications(tmp_path / "nope.db", 30, NOW) == []


def test_read_missing_table_returns_empty(tmp_path):
    db = tmp_path / "g.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    assert pr_watch.read_steward_notifications(db, 30, NOW) == []


# --------------------------------------------------------------------------
# sidecar load/save
# --------------------------------------------------------------------------


def test_sidecar_absent(tmp_path):
    surfaced, existed = pr_watch.load_sidecar(tmp_path / "seen.json")
    assert surfaced == {} and existed is False


def test_sidecar_corrupt(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("{not json")
    surfaced, existed = pr_watch.load_sidecar(p)
    assert surfaced == {} and existed is False


def test_sidecar_roundtrip(tmp_path):
    p = tmp_path / "sub" / "seen.json"
    pr_watch.save_sidecar(p, {"x": {"first_ts": NOW.isoformat()}})
    surfaced, existed = pr_watch.load_sidecar(p)
    assert existed is True
    assert surfaced["x"]["first_ts"] == NOW.isoformat()
    # File is valid JSON with the version envelope.
    data = json.loads(p.read_text())
    assert data["version"] == 1


# --------------------------------------------------------------------------
# select_to_surface
# --------------------------------------------------------------------------


def _notif(nid, days_ago):
    return {
        "id": nid,
        "topic": f"PR steward tick: {nid}",
        "delivered_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


def test_select_first_sight_surfaces_and_records(tmp_path):
    notifs = [_notif("a", 1)]
    lines, new = pr_watch.select_to_surface(notifs, {}, NOW, 10, 5)
    assert len(lines) == 1
    assert new["a"]["first_ts"] == NOW.isoformat()


def test_select_resurfaces_within_window():
    notifs = [_notif("a", 1)]
    surfaced = {"a": {"first_ts": (NOW - timedelta(days=3)).isoformat()}}
    lines, new = pr_watch.select_to_surface(notifs, surfaced, NOW, 10, 5)
    assert len(lines) == 1
    # first_ts is preserved (not reset), so the window keeps counting down.
    assert new["a"]["first_ts"] == surfaced["a"]["first_ts"]


def test_select_ages_out_beyond_window_but_keeps_record():
    notifs = [_notif("a", 20)]
    surfaced = {"a": {"first_ts": (NOW - timedelta(days=15)).isoformat()}}
    lines, new = pr_watch.select_to_surface(notifs, surfaced, NOW, 10, 5)
    assert lines == []
    assert "a" in new  # kept so it does not re-surface as "new"


def test_select_prunes_ids_outside_current_notifs():
    notifs = [_notif("a", 1)]
    surfaced = {"gone": {"first_ts": (NOW - timedelta(days=2)).isoformat()}}
    _lines, new = pr_watch.select_to_surface(notifs, surfaced, NOW, 10, 5)
    assert "gone" not in new  # pruned (self-bounding sidecar)


def test_select_caps_at_max_surface_with_overflow():
    notifs = [_notif(str(i), 1) for i in range(8)]
    lines, _new = pr_watch.select_to_surface(notifs, {}, NOW, 10, 3)
    assert len(lines) == 4  # 3 clauses + 1 overflow marker
    assert lines[-1] == "+5 more"


# --------------------------------------------------------------------------
# render_clause / format_injection
# --------------------------------------------------------------------------


def test_render_clause_takes_first_line_and_dates():
    clause = pr_watch.render_clause(
        {"topic": "PR steward: #905 closed\n\nmore detail", "delivered_at": NOW.isoformat()}
    )
    assert clause.startswith("PR steward: #905 closed")
    assert "Jul 21" in clause


def test_render_clause_truncates_long_topic():
    clause = pr_watch.render_clause({"topic": "x" * 200, "delivered_at": None})
    assert len(clause) <= 73 and clause.endswith("…")


def test_format_injection_empty():
    assert pr_watch.format_injection([]) == ""


def test_format_injection_singular_plural_and_overflow():
    assert "1 external-PR update " in pr_watch.format_injection(["one (Jul 1)"])
    two = pr_watch.format_injection(["a (Jul 1)", "b (Jul 2)"])
    assert "2 external-PR updates " in two
    # Overflow marker does not inflate the count.
    with_overflow = pr_watch.format_injection(["a (Jul 1)", "+4 more"])
    assert "1 external-PR update " in with_overflow
