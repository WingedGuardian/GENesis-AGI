"""Pure-function tests for the OMI transcript normalizer.

Covers the three pure pieces of ``omi_normalize``:
  * ``parse_payload`` — tolerate the real wire shape (object with ``segments`` +
    ``session_id``) AND a bare-array fallback, both casings of ``speaker_id``.
  * ``normalize_segments`` — map an OMI segment onto an ``ambient_transcripts``
    row (byte-compatible with home snapshots), NEVER emitting a ``meta.audio``
    block (that is what keeps OMI rows on PR-4's text-only clarity path).
  * ``decide_anchor`` — the per-uid re-anchor rule that turns OMI's
    conversation-relative ``start``/``end`` into wall-clock timestamps.

All wall-clock independent: every timestamp is injected.
"""
import json

import pytest

from genesis.attention.omi_normalize import (
    NormalizedRow,
    decide_anchor,
    normalize_segments,
    parse_payload,
)

# A representative real segment (fields traced from the OMI sender source).
_SEG = {
    "id": "seg-uuid-1",
    "text": "hello there world",
    "speaker": "SPEAKER_0",
    "speaker_id": 0,
    "is_user": False,
    "person_id": None,
    "start": 2.0,
    "end": 5.5,
    "stt_provider": "deepgram",
}


# ── parse_payload ──────────────────────────────────────────────────────────
def test_parse_payload_object_shape():
    uid, segs = parse_payload({"segments": [_SEG], "session_id": "acct-uid"})
    assert uid == "acct-uid"
    assert segs == [_SEG]


def test_parse_payload_bare_array_has_no_session_id():
    uid, segs = parse_payload([_SEG, _SEG])
    assert uid is None
    assert len(segs) == 2


def test_parse_payload_object_without_segments_is_empty():
    uid, segs = parse_payload({"session_id": "acct-uid"})
    assert uid == "acct-uid"
    assert segs == []


def test_parse_payload_non_container_is_empty():
    assert parse_payload("garbage") == (None, [])
    assert parse_payload(None) == (None, [])
    assert parse_payload(42) == (None, [])


def test_parse_payload_filters_non_dict_segments():
    uid, segs = parse_payload({"segments": [_SEG, "junk", None, 7]})
    assert segs == [_SEG]


# ── normalize_segments ─────────────────────────────────────────────────────
def test_normalize_basic_mapping():
    rows = normalize_segments([_SEG], uid="acct-uid", epoch0=1000.0)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, NormalizedRow)
    assert row.segment_id == "seg-uuid-1"
    assert row.text == "hello there world"
    assert row.speaker_label == "SPEAKER_0"
    assert row.duration_s == pytest.approx(3.5)  # 5.5 - 2.0
    assert row.source == "omi"
    assert row.provenance == "omi_webhook"
    assert row.speaker_name is None
    assert row.is_user == 0  # False -> 0


def test_normalize_ts_is_epoch0_plus_start_utc_iso():
    rows = normalize_segments([_SEG], uid="u", epoch0=1_000_000.0)
    # epoch0 + start = 1_000_002.0; must render UTC ISO with +00:00 offset
    assert rows[0].ts.endswith("+00:00")
    from datetime import UTC, datetime

    expected = datetime.fromtimestamp(1_000_002.0, UTC).isoformat()
    assert rows[0].ts == expected


def test_normalize_meta_has_omi_and_asr_feats_but_no_audio():
    rows = normalize_segments([_SEG], uid="acct-uid", epoch0=0.0)
    meta = json.loads(rows[0].meta)
    assert "audio" not in meta  # critical: keeps has_audio=False text-only path
    assert meta["omi"] == {
        "uid": "acct-uid",
        "segment_id": "seg-uuid-1",
        "speaker_id": 0,
        "person_id": None,
        "stt_provider": "deepgram",
    }
    assert meta["asr_feats"]["n_tokens"] == 3  # "hello there world"


def test_normalize_skips_empty_and_whitespace_text():
    segs = [
        {**_SEG, "id": "a", "text": ""},
        {**_SEG, "id": "b", "text": "   "},
        {**_SEG, "id": "c", "text": "real words"},
    ]
    rows = normalize_segments(segs, uid="u", epoch0=0.0)
    assert [r.segment_id for r in rows] == ["c"]


def test_normalize_clamps_negative_duration_to_zero():
    seg = {**_SEG, "start": 5.0, "end": 2.0}  # end before start
    rows = normalize_segments([seg], uid="u", epoch0=0.0)
    assert rows[0].duration_s == 0.0


def test_normalize_tolerates_camelcase_speaker_id():
    seg = {k: v for k, v in _SEG.items() if k != "speaker_id"}
    seg["speakerId"] = 4
    rows = normalize_segments([seg], uid="u", epoch0=0.0)
    meta = json.loads(rows[0].meta)
    assert meta["omi"]["speaker_id"] == 4


def test_normalize_missing_optional_fields_are_none():
    seg = {"id": "x", "text": "hi", "start": 0.0, "end": 1.0}
    rows = normalize_segments([seg], uid="u", epoch0=0.0)
    row = rows[0]
    assert row.speaker_label is None
    assert row.is_user is None
    meta = json.loads(row.meta)
    assert meta["omi"]["speaker_id"] is None
    assert meta["omi"]["person_id"] is None
    assert meta["omi"]["stt_provider"] is None


def test_normalize_is_user_true_maps_to_one():
    rows = normalize_segments([{**_SEG, "is_user": True}], uid="u", epoch0=0.0)
    assert rows[0].is_user == 1


# ── decide_anchor ──────────────────────────────────────────────────────────
def test_decide_anchor_no_state_anchors_to_recv_minus_end():
    # First batch ever for this uid: epoch0 = recv_ts - batch_max_end
    assert decide_anchor(None, batch_max_end=5.0, recv_ts=1000.0, tolerance=60.0) == 995.0


def test_decide_anchor_in_window_keeps_existing_epoch0():
    # epoch0=995 => predicted wall of this batch = 995 + 10 = 1005, recv=1006
    # skew 1s < tolerance -> keep 995
    assert decide_anchor(995.0, batch_max_end=10.0, recv_ts=1006.0, tolerance=60.0) == 995.0


def test_decide_anchor_out_of_window_reanchors():
    # Conversation rollover: start/end reset to ~0 while wall clock jumped hours.
    # predicted = 995 + 2 = 997, recv = 9000 -> skew 8003 > tol -> re-anchor.
    assert decide_anchor(995.0, batch_max_end=2.0, recv_ts=9000.0, tolerance=60.0) == 8998.0


def test_decide_anchor_boundary_exactly_tolerance_keeps():
    # skew == tolerance is NOT > tolerance -> keep (re-anchor only on strict exceed)
    # predicted = 100 + 0 = 100, recv = 160, skew = 60 == tol -> keep 100
    assert decide_anchor(100.0, batch_max_end=0.0, recv_ts=160.0, tolerance=60.0) == 100.0
