"""ambient_transcripts row -> AmbientUtterance mapping (pure, no DB) + snapshot text reveal."""
import json
import sqlite3

import pytest

from genesis.attention.sources import (
    _parse_ts,
    _speaker_total,
    resolve_window_text,
    row_to_utterance,
)


def _row(**over) -> dict:
    row = {
        "id": 42, "ts": "2026-06-30T12:00:00+00:00", "text": "hey genesis",
        "duration_s": 2.5, "speaker_label": "w7:2/3", "source": "dev1",
        "is_user": 1, "speaker_name": "user",
        "meta": json.dumps({
            "asr_feats": {"ys_log_probs": [-0.2, -1.5, -2.0, -0.1], "n_tokens": 4},
            "audio": {"rms": 0.15},
        }),
    }
    row.update(over)
    return row


def test_row_to_utterance_full():
    u = row_to_utterance(_row())
    assert u.id == 42 and u.is_user == 1
    assert u.speaker_total == 3       # "w7:2/3" -> 3
    assert u.n_tokens == 4
    assert u.rms == 0.15
    assert u.frac_lt_1 == 0.5         # two of four ys_log_probs < -1.0
    assert u.mode_state == "unknown"  # ambient carries no interaction-plane state


def test_row_missing_meta_and_label():
    u = row_to_utterance(_row(meta=None, speaker_label=None, is_user=None))
    assert u.speaker_total is None and u.n_tokens == 0 and u.frac_lt_1 == 0.0 and u.rms == 0.0


def test_row_bad_ts_returns_none():
    assert row_to_utterance(_row(ts="not-a-date")) is None
    assert row_to_utterance(_row(ts=None)) is None


def test_row_corrupt_meta_tolerated():
    u = row_to_utterance(_row(meta="{not valid json"))
    assert u is not None and u.n_tokens == 0 and u.rms == 0.0


def test_speaker_total_parse():
    assert _speaker_total("w500:2/5") == 5
    assert _speaker_total("w1:1/1") == 1
    assert _speaker_total(None) is None
    assert _speaker_total("weird") is None


def test_parse_ts_naive_assumed_utc_equals_explicit_utc():
    assert _parse_ts("2026-06-30T12:00:00") == _parse_ts("2026-06-30T12:00:00+00:00")


# ── resolve_window_text: reveal transcript text from the transient snapshot ──────

def _make_snapshot(dir_, snapshot_id, rows) -> None:
    conn = sqlite3.connect(dir_ / f"ambient_{snapshot_id}.db")
    conn.execute(
        "CREATE TABLE ambient_transcripts (id INTEGER PRIMARY KEY, ts TEXT, text TEXT, "
        "duration_s REAL, speaker_label TEXT, provenance TEXT, source TEXT, meta TEXT, "
        "is_user INTEGER, speaker_name TEXT)"
    )
    conn.executemany(
        "INSERT INTO ambient_transcripts (id, ts, text, speaker_label, is_user) "
        "VALUES (?, ?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_resolve_window_text_orders_and_flags_trigger(tmp_path):
    _make_snapshot(tmp_path, "20260701T013412Z", [
        (10, "2026-07-01T00:00:02+00:00", "second", "w1:1/2", 0),
        (11, "2026-07-01T00:00:01+00:00", "first", "w1:1/2", 1),
        (12, "2026-07-01T00:00:03+00:00", "trigger utt", "w1:1/2", 0),
    ])
    win = await resolve_window_text("20260701T013412Z", [11, 10, 12], snapshots_dir=tmp_path)
    assert [w["text"] for w in win] == ["first", "second", "trigger utt"]  # ts-ordered
    assert [w["is_trigger"] for w in win] == [False, False, True]          # last utt_id
    assert win[0]["is_user"] == 1 and win[0]["speaker_label"] == "w1:1/2"


@pytest.mark.asyncio
async def test_resolve_window_text_missing_snapshot_returns_none(tmp_path):
    assert await resolve_window_text("gone", [1, 2], snapshots_dir=tmp_path) is None


@pytest.mark.asyncio
async def test_resolve_window_text_bare_filename_form(tmp_path):
    # tolerate a snapshot stored WITHOUT the ambient_ prefix
    conn = sqlite3.connect(tmp_path / "bareid.db")
    conn.execute("CREATE TABLE ambient_transcripts (id INTEGER PRIMARY KEY, ts TEXT, text TEXT, "
                 "speaker_label TEXT, is_user INTEGER)")
    conn.execute("INSERT INTO ambient_transcripts VALUES (1, '2026-07-01T00:00:00+00:00', 'x', 'w1', 1)")
    conn.commit()
    conn.close()
    win = await resolve_window_text("bareid", [1], snapshots_dir=tmp_path)
    assert win and win[0]["text"] == "x"


@pytest.mark.asyncio
async def test_resolve_window_text_rejects_non_int_ids(tmp_path):
    _make_snapshot(tmp_path, "sid", [(1, "2026-07-01T00:00:00+00:00", "x", "w1", 1)])
    with pytest.raises(ValueError):
        await resolve_window_text("sid", ["1; DROP TABLE ambient_transcripts"], snapshots_dir=tmp_path)
