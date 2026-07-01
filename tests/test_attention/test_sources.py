"""ambient_transcripts row -> AmbientUtterance mapping (pure, no DB)."""
import json

from genesis.attention.sources import _parse_ts, _speaker_total, row_to_utterance


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
