"""differ: PURE A/B fire-set diff + row/event mappers + collector + load_from_db (temp RO DB).

The rescore IO path (run_shadow + _Collector over a real snapshot) is covered by the
mid-build real-corpus checkpoint / E2E, not a unit — here we unit its pure pieces
(_record_from_event, _Collector) and diff logic."""
import json

import aiosqlite
import pytest

from genesis.attention.consumers import ShadowConsumer
from genesis.attention.differ import (
    FireRecord,
    _Collector,
    _record_from_event,
    _record_from_row,
    diff_fire_sets,
    format_diff,
    load_from_db,
)
from genesis.attention.types import (
    Activation,
    AttentionEvent,
    TriggerHit,
    TriggerKind,
    WindowRef,
)
from genesis.db.crud import attention as crud
from genesis.db.schema._tables import INDEXES, TABLES


def _fr(key, activation="soft", triggers=("multi_speaker",), suppressors=()):
    return FireRecord(key, activation, frozenset(triggers), frozenset(suppressors))


# ── pure diff (the tested heart) ─────────────────────────────────────────────

def test_identical_sets_no_diff():
    a = [_fr(1), _fr(2, triggers=("question",))]
    d = diff_fire_sets(a, list(a))
    assert d.added == [] and d.removed == [] and d.activation_changed == []
    assert all(delta == 0 for _, _, delta in d.by_trigger_delta.values())
    assert d.baseline_n == 2 and d.candidate_n == 2


def test_pure_add():
    d = diff_fire_sets([_fr(1)], [_fr(1), _fr(2, triggers=("decision",))])
    assert [r.key for r in d.added] == [2] and d.removed == []
    assert d.by_trigger_delta["decision"] == (0, 1, 1)


def test_pure_remove():
    d = diff_fire_sets([_fr(1), _fr(2)], [_fr(1)])
    assert [r.key for r in d.removed] == [2] and d.added == []


def test_activation_change_is_not_add_remove():
    d = diff_fire_sets([_fr(5, activation="soft")], [_fr(5, activation="suppressed")])
    assert d.added == [] and d.removed == []
    assert d.activation_changed == [(5, "soft", "suppressed")]


def test_by_trigger_delta_counts_per_record():
    base = [_fr(1, triggers=("multi_speaker", "question")), _fr(2, triggers=("multi_speaker",))]
    cand = [_fr(1, triggers=("multi_speaker",))]  # utt2 dropped; utt1 lost 'question'
    d = diff_fire_sets(base, cand)
    assert d.by_trigger_delta["multi_speaker"] == (2, 1, -1)
    assert d.by_trigger_delta["question"] == (1, 0, -1)


def test_empty_sides():
    d = diff_fire_sets([], [])
    assert d.added == [] and d.removed == [] and d.by_trigger_delta == {}
    assert d.by_suppressor_delta == {}
    assert d.baseline_n == 0 and d.candidate_n == 0


def test_by_suppressor_delta():
    base = [_fr(1, activation="soft", suppressors=())]
    cand = [_fr(1, activation="suppressed", suppressors=("explicit_dismissal",))]
    d = diff_fire_sets(base, cand)
    assert d.by_suppressor_delta == {"explicit_dismissal": (0, 1, 1)}
    assert d.activation_changed == [(1, "soft", "suppressed")]  # the suppression also shows as an activation change


def test_added_and_removed_sorted_by_key():
    d = diff_fire_sets([_fr(8)], [_fr(9), _fr(3), _fr(7)])
    assert [r.key for r in d.added] == [3, 7, 9]
    assert [r.key for r in d.removed] == [8]


def test_labels_and_unresolvable_passthrough():
    d = diff_fire_sets(
        [], [], baseline_label="0.1.0-default", candidate_label="rescore:0.2.0-taxonomy",
        unresolvable={"0.1.0-default": 2},
    )
    assert d.baseline_label == "0.1.0-default"
    assert d.candidate_label == "rescore:0.2.0-taxonomy"
    assert d.unresolvable == {"0.1.0-default": 2}


def test_format_diff_is_text_only_no_crash():
    out = format_diff(diff_fire_sets([_fr(1)], [_fr(2, triggers=("decision",))]))
    assert "FIRE-SET DIFF" in out and "decision" in out and "utt 2" in out


# ── mappers ──────────────────────────────────────────────────────────────────

def _row_dict(
    utt_ids=(21051,),
    activation="soft",
    triggers=(("question", "soft", 0.3), ("multi_speaker", "soft", 0.4)),
    suppressors=(),
):
    return {
        "id": "x",
        "activation": activation,
        "triggers_fired": json.dumps([{"name": n, "kind": k, "contribution": c} for n, k, c in triggers]),
        "suppressors": json.dumps(list(suppressors)),
        "window_ref": json.dumps(
            {"snapshot_id": "s", "session_id": "s1", "utt_ids": list(utt_ids), "ts_start": 0.0, "ts_end": 1.0}
        ),
    }


def test_record_from_row_happy():
    assert _record_from_row(_row_dict()) == FireRecord(
        21051, "soft", frozenset({"question", "multi_speaker"}), frozenset()
    )


def test_record_from_row_keys_on_last_utt():
    assert _record_from_row(_row_dict(utt_ids=(10, 20, 30))).key == 30


def test_record_from_row_carries_suppressors():
    r = _record_from_row(_row_dict(activation="suppressed", suppressors=("explicit_dismissal",)))
    assert r.activation == "suppressed" and r.suppressors == frozenset({"explicit_dismissal"})


def test_record_from_row_empty_utt_ids_none():
    assert _record_from_row(_row_dict(utt_ids=())) is None


def test_record_from_row_null_json_columns_safe():
    assert _record_from_row(
        {"id": "x", "activation": "soft", "window_ref": None, "triggers_fired": None, "suppressors": None}
    ) is None


def _event(utt_ids=(5,), activation=Activation.SOFT, triggers=("question",), suppressors=()):
    return AttentionEvent(
        activation=activation, score=0.6,
        triggers_fired=tuple(TriggerHit(n, TriggerKind.SOFT, 0.3) for n in triggers),
        suppressors=tuple(suppressors), session_id="s1",
        window_ref=WindowRef("s1", tuple(utt_ids), 0.0, 1.0), ts=1.0, mode_state="unknown", clarity=0.9,
    )


def test_record_from_event_happy():
    assert _record_from_event(_event(utt_ids=(5,), triggers=("question", "is_user"))) == FireRecord(
        5, "soft", frozenset({"question", "is_user"}), frozenset()
    )


def test_record_from_event_no_window_none():
    assert _record_from_event(_event(utt_ids=())) is None


# ── collector satisfies the ShadowConsumer contract ──────────────────────────

def test_collector_is_a_shadow_consumer():
    assert isinstance(_Collector(), ShadowConsumer)


@pytest.mark.asyncio
async def test_collector_captures_events_and_flush_persists_nothing():
    c = _Collector()
    c.add(_event(utt_ids=(1,)))
    c.add(_event(utt_ids=(2,)))
    assert [e.window_ref.utt_ids[-1] for e in c.events] == [1, 2]
    assert await c.flush() == 0  # differ reads c.events; nothing written to any DB


# ── load_from_db (own read-only connection, version-scoped) ──────────────────

async def _seed_db(path, rows):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute(TABLES["attention_events"])
    for idx in INDEXES:
        if "attention_events" in idx:
            await conn.execute(idx)
    await conn.commit()
    await crud.bulk_upsert_events(conn, rows)
    await conn.close()


def _full_row(id_, config_version, *, utt_ids=(1, 2, 3), activation="soft",
              triggers=(("multi_speaker", "soft", 0.4),)):
    """A full attention_events tuple in COLUMNS order (bypasses _to_row so utt_ids can be empty)."""
    triggers_json = json.dumps([{"name": n, "kind": k, "contribution": c} for n, k, c in triggers])
    window_ref = json.dumps(
        {"snapshot_id": "s", "session_id": "s1", "utt_ids": list(utt_ids), "ts_start": 0.0, "ts_end": 1.0}
    )
    return (
        id_, "2026-07-01T00:00:00+00:00", "s1", activation, 0.6, triggers_json,
        json.dumps([]), window_ref, "unknown", 0.9, None, None, "s", config_version,
        "2026-07-01T00:00:00+00:00", "",
    )


@pytest.mark.asyncio
async def test_load_from_db_scopes_to_version_and_maps(tmp_path):
    p = tmp_path / "g.db"
    await _seed_db(p, [
        _full_row("a", "0.1.0-default", utt_ids=(11,)),
        _full_row("b", "0.1.0-default", utt_ids=(22,), triggers=(("question", "soft", 0.3),)),
        _full_row("c", "0.2.0-taxonomy", utt_ids=(33,)),
    ])
    recs, unres = await load_from_db(p, "0.1.0-default")
    assert unres == 0
    assert {r.key for r in recs} == {11, 22}  # the 0.2.0 row is excluded
    assert next(r for r in recs if r.key == 22).triggers == frozenset({"question"})


@pytest.mark.asyncio
async def test_load_from_db_counts_unresolvable_empty_utt_ids(tmp_path):
    p = tmp_path / "g.db"
    await _seed_db(p, [
        _full_row("a", "0.1.0-default", utt_ids=(11,)),
        _full_row("b", "0.1.0-default", utt_ids=()),  # empty utt_ids -> skipped + counted
    ])
    recs, unres = await load_from_db(p, "0.1.0-default")
    assert {r.key for r in recs} == {11} and unres == 1
