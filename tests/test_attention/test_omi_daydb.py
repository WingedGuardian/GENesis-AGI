"""Tests for the OMI per-UTC-day snapshot-db writer.

The day-db is byte-compatible with a home ``ambient_*.db`` snapshot — it contains
ONLY the exact 10-column ``ambient_transcripts`` table — so the unchanged
``SnapshotSource`` / ``run_shadow`` / reveal / GC machinery consumes it. Row-level
dedup is NOT the day-db's job (the schema has no segment_id column); the ingest
orchestrator filters via ``omi_state.seen_segments`` before calling ``insert_rows``.
So the day-db is append-only and this file tests the schema, the filename/id
contract with the GC + runner, and a real round-trip through the reader.
"""
import re
import sqlite3
from datetime import datetime

from genesis.attention import omi_daydb, snapshot
from genesis.attention.omi_normalize import normalize_segments
from genesis.attention.sources import SnapshotSource

_GC_RE = re.compile(r"^omi_\d{8}\.db$")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def test_snapshot_id_and_filename_match_gc_regex():
    recv = _epoch("2026-07-07T15:30:00+00:00")
    assert omi_daydb.snapshot_id_for(recv) == "omi_20260707"
    path = omi_daydb.day_db_path(recv, snapshots_dir="/tmp/x")
    assert _GC_RE.match(path.name)  # exactly what scripts/attention_snapshot_gc.py wants


def test_snapshot_id_is_utc_not_local():
    # 23:59 UTC and 00:01 UTC next day fall on different UTC days regardless of TZ.
    assert omi_daydb.snapshot_id_for(_epoch("2026-07-07T23:59:00+00:00")) == "omi_20260707"
    assert omi_daydb.snapshot_id_for(_epoch("2026-07-08T00:01:00+00:00")) == "omi_20260708"


def test_ensure_schema_exact_ten_columns(tmp_path):
    recv = _epoch("2026-07-07T12:00:00+00:00")
    rows = normalize_segments(
        [{"id": "s1", "text": "hi there", "speaker": "SPEAKER_0", "start": 0.0, "end": 1.0}],
        uid="u",
        epoch0=recv,
    )
    omi_daydb.insert_rows(rows, recv_ts=recv, snapshots_dir=tmp_path)
    db = tmp_path / "omi_20260707.db"
    conn = sqlite3.connect(str(db))
    cols = [(r[1], r[2], r[3], r[5]) for r in conn.execute("PRAGMA table_info(ambient_transcripts)")]
    names = [c[0] for c in cols]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert names == [
        "id", "ts", "text", "duration_s", "speaker_label",
        "provenance", "source", "meta", "is_user", "speaker_name",
    ]
    # id is the autoincrement PK; ts/text/provenance are NOT NULL
    assert dict((c[0], c[3]) for c in cols)["id"] == 1  # pk
    assert "idx_ambient_ts" in idx
    # Only ambient_transcripts (+ sqlite_sequence from AUTOINCREMENT); no side tables.
    assert tables == {"ambient_transcripts", "sqlite_sequence"}


def test_roundtrip_through_snapshotsource(tmp_path):
    recv = _epoch("2026-07-07T12:00:00+00:00")
    segs = [
        {"id": "s1", "text": "first utterance here", "speaker": "SPEAKER_0",
         "is_user": True, "start": 1.0, "end": 3.0},
        {"id": "s2", "text": "second one", "speaker": "SPEAKER_1",
         "is_user": False, "start": 4.0, "end": 5.0},
    ]
    rows = normalize_segments(segs, uid="acct", epoch0=recv)
    n = omi_daydb.insert_rows(rows, recv_ts=recv, snapshots_dir=tmp_path)
    assert n == 2

    src = SnapshotSource(tmp_path / "omi_20260707.db")
    utts = list(src.iter_utterances())
    assert [u.text for u in utts] == ["first utterance here", "second one"]  # ts order
    assert all(u.source == "omi" for u in utts)
    assert all(u.has_audio is False for u in utts)  # text-only clarity path
    assert utts[0].n_tokens == 3  # "first utterance here"
    assert utts[0].is_user == 1
    assert utts[0].rms == 0.0  # no audio block -> rms unknown, not penalized


def test_append_across_calls_same_day(tmp_path):
    recv = _epoch("2026-07-07T12:00:00+00:00")
    r1 = normalize_segments([{"id": "a", "text": "one", "start": 0.0, "end": 1.0}], uid="u", epoch0=recv)
    r2 = normalize_segments([{"id": "b", "text": "two", "start": 2.0, "end": 3.0}], uid="u", epoch0=recv)
    omi_daydb.insert_rows(r1, recv_ts=recv, snapshots_dir=tmp_path)
    omi_daydb.insert_rows(r2, recv_ts=recv + 2, snapshots_dir=tmp_path)  # same UTC day
    utts = list(SnapshotSource(tmp_path / "omi_20260707.db").iter_utterances())
    assert [u.text for u in utts] == ["one", "two"]


def test_insert_empty_rows_is_noop(tmp_path):
    recv = _epoch("2026-07-07T12:00:00+00:00")
    assert omi_daydb.insert_rows([], recv_ts=recv, snapshots_dir=tmp_path) == 0
    assert not (tmp_path / "omi_20260707.db").exists()  # no file created for nothing


def test_snapshots_dir_pinned_to_engine_default():
    # No user-facing snapshots_dir knob: the write side is the SAME dir the
    # reader / GC / runner are pinned to. This guards against drift.
    assert omi_daydb.SNAPSHOTS_DIR == snapshot._DEFAULT_DEST
