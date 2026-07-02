"""calibrate (PR3c-2a): PURE precision metrics + row→LabeledFire mapper + Wilson + report,
plus a seeded-DB ``load_labeled`` integration. The DB/LLM-free pure heart is what these exercise;
the metric definitions are the load-bearing correctness surface (a subtly-wrong metric misleads
weight tuning), so each is pinned including the architect-caught traps:
  - zero-weight "ghost" triggers (topic_continuation @ contribution 0) must NOT earn precision,
  - threshold sweep splits soft-only vs combined + guards the soft-recall zero-denominator,
  - suppressor stats report raw counts (ratio only at n>=LOW_N),
  - NULL clarity gets its own bucket.
"""
import json

import aiosqlite
import pytest

from genesis.attention.calibrate import (
    LOW_N,
    LabeledFire,
    PrecisionStat,
    cooccurring_triggers,
    format_report,
    labeled_from_row,
    load_labeled,
    overall_precision,
    precision_by_activation,
    precision_by_clarity_band,
    precision_by_trigger,
    suppressor_stats,
    threshold_sweep,
    wilson_interval,
)
from genesis.db.crud import attention as crud
from genesis.db.schema._tables import INDEXES, TABLES


def _lf(key, signal="should", *, activation="soft", score=0.65, clarity=0.9,
        causal=("multi_speaker",), hard=(), inert=(), suppressors=()):
    return LabeledFire(
        key=key, activation=activation, score=score, clarity=clarity, signal=signal,
        causal_soft=frozenset(causal), hard_triggers=frozenset(hard),
        inert_soft=frozenset(inert), suppressors=frozenset(suppressors),
    )


# ── wilson_interval ──────────────────────────────────────────────────────────────────

def test_wilson_none_at_zero_n():
    assert wilson_interval(0, 0) is None


def test_wilson_perfect_small_n_is_not_1():
    lo, hi = wilson_interval(2, 2)
    assert hi == pytest.approx(1.0, abs=1e-9)
    assert 0.2 < lo < 0.5           # 2/2 is NOT certainty — lower bound ~0.34


def test_wilson_zero_successes():
    lo, hi = wilson_interval(0, 2)
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert 0.5 < hi < 0.8           # upper bound ~0.66


def test_wilson_tightens_with_n():
    _, hi_small = wilson_interval(8, 10)
    _, hi_big = wilson_interval(80, 100)
    assert hi_big < hi_small        # same ratio, more data -> tighter


# ── PrecisionStat ────────────────────────────────────────────────────────────────────

def test_precision_stat_props():
    s = PrecisionStat(should=6, shouldnt=2)
    assert s.n == 8 and s.precision == pytest.approx(0.75) and s.low_n is True
    assert PrecisionStat(0, 0).precision is None and PrecisionStat(0, 0).wilson is None
    assert PrecisionStat(15, 5).low_n is False


# ── labeled_from_row (the causal/hard/inert split — the architect trap) ──────────────

def _row(*, utt_ids=(21051,), activation="soft", score=0.65, clarity=0.9, signal="should",
         triggers=(("question", "soft", 0.3), ("multi_speaker", "soft", 0.4)), suppressors=()):
    return {
        "id": "x", "activation": activation, "score": score, "clarity": clarity,
        "acceptance_signal": signal,
        "triggers_fired": json.dumps([{"name": n, "kind": k, "contribution": c} for n, k, c in triggers]),
        "suppressors": json.dumps(list(suppressors)),
        "window_ref": json.dumps({"utt_ids": list(utt_ids)}),
    }


def test_row_splits_causal_hard_inert():
    # topic_continuation @ 0.0 (inert), ambient_name hard (contribution 0.0 but HARD), question causal
    lf = labeled_from_row(_row(activation="hard", triggers=(
        ("ambient_name", "hard", 0.0),
        ("question", "soft", 0.3),
        ("topic_continuation", "soft", 0.0),
    )))
    assert lf.hard_triggers == frozenset({"ambient_name"})
    assert lf.causal_soft == frozenset({"question"})
    assert lf.inert_soft == frozenset({"topic_continuation"})
    assert lf.causal == frozenset({"ambient_name", "question"})   # inert NOT in causal


def test_row_keys_on_last_utt_and_reads_score():
    lf = labeled_from_row(_row(utt_ids=(10, 20, 30), score=0.72))
    assert lf.key == 30 and lf.score == pytest.approx(0.72)


def test_row_empty_utt_ids_none():
    assert labeled_from_row(_row(utt_ids=())) is None


def test_row_null_clarity_passthrough():
    assert labeled_from_row(_row(clarity=None)).clarity is None


def test_row_null_json_columns_safe():
    lf = labeled_from_row({"activation": "soft", "score": 0.6, "clarity": 0.9,
                           "acceptance_signal": "should", "window_ref": json.dumps({"utt_ids": [5]}),
                           "triggers_fired": None, "suppressors": None})
    assert lf.causal_soft == frozenset() and lf.suppressors == frozenset()


# ── precision metrics ────────────────────────────────────────────────────────────────

def test_overall_precision_excludes_suppressed():
    fires = [_lf(1, "should"), _lf(2, "shouldnt"), _lf(3, "should", activation="suppressed")]
    s = overall_precision(fires)
    assert s.should == 1 and s.shouldnt == 1        # suppressed row not counted


def test_precision_by_activation():
    fires = [_lf(1, "should", activation="hard"), _lf(2, "shouldnt", activation="soft"),
             _lf(3, "should", activation="soft")]
    by = precision_by_activation(fires)
    assert by["hard"].should == 1 and by["hard"].shouldnt == 0
    assert by["soft"].should == 1 and by["soft"].shouldnt == 1


def test_precision_by_trigger_uses_causal_only():
    # topic_continuation is inert on both fires; it must NOT appear in precision_by_trigger.
    fires = [
        _lf(1, "should", causal=("multi_speaker", "question"), inert=("topic_continuation",)),
        _lf(2, "shouldnt", causal=("multi_speaker",), inert=("topic_continuation",)),
    ]
    by = precision_by_trigger(fires)
    assert "topic_continuation" not in by
    assert by["multi_speaker"].should == 1 and by["multi_speaker"].shouldnt == 1
    assert by["question"].should == 1 and by["question"].shouldnt == 0


def test_cooccurring_triggers_counts_inert():
    fires = [_lf(1, inert=("topic_continuation",)), _lf(2, inert=("topic_continuation",))]
    assert cooccurring_triggers(fires) == {"topic_continuation": 2}


def test_suppressor_stats_polarity_and_low_n():
    # should = wrongly-vetoed MISS; shouldnt = correct veto. n<LOW_N -> rate None.
    fires = [_lf(1, "should", activation="suppressed", suppressors=("mode_active_listen",)),
             _lf(2, "shouldnt", activation="suppressed", suppressors=("mode_active_listen",))]
    st = suppressor_stats(fires)["mode_active_listen"]
    assert st.n_miss == 1 and st.n_correct_veto == 1 and st.n == 2
    assert st.low_n is True and st.correct_veto_rate is None


def test_suppressor_stats_rate_at_threshold():
    fires = [_lf(i, "shouldnt", activation="suppressed", suppressors=("x",)) for i in range(LOW_N)]
    st = suppressor_stats(fires)["x"]
    assert st.n == LOW_N and st.correct_veto_rate == pytest.approx(1.0)


# ── clarity bands ────────────────────────────────────────────────────────────────────

def test_clarity_bands_bucket_and_null():
    fires = [_lf(1, clarity=0.65), _lf(2, clarity=0.9), _lf(3, clarity=None), _lf(4, clarity=1.0)]
    bands = precision_by_clarity_band(fires)
    assert bands["[0.0,0.7)"].n == 1
    assert bands["[0.85,1.0]"].n == 2           # 0.9 and 1.0 (top band closed)
    assert bands["[0.7,0.85)"].n == 0           # empty band still reported, n=0
    assert bands["clarity=NULL"].n == 1         # NULL gets its own bucket, not folded low


# ── threshold sweep ──────────────────────────────────────────────────────────────────

def test_threshold_sweep_monotone_and_soft_vs_combined():
    fires = [
        _lf(1, "should", activation="soft", score=0.62),
        _lf(2, "shouldnt", activation="soft", score=0.70),
        _lf(3, "should", activation="hard", score=0.0),   # hard: threshold-independent
    ]
    pts = {p.t: p for p in threshold_sweep(fires, [0.60, 0.65, 0.72])}
    assert pts[0.60].n_soft_retained == 2
    assert pts[0.65].n_soft_retained == 1        # 0.62 dropped
    assert pts[0.72].n_soft_retained == 0        # both dropped -> soft precision None
    assert pts[0.72].soft.precision is None
    # combined always includes the hard should-fire
    assert pts[0.72].combined.should == 1 and pts[0.72].combined.n == 1


def test_threshold_sweep_score_equal_t_retained():
    fires = [_lf(1, "should", activation="soft", score=0.70)]
    assert threshold_sweep(fires, [0.70])[0].n_soft_retained == 1   # >= boundary


def test_threshold_sweep_soft_recall_guarded():
    # no should-labeled SOFT fires -> soft_recall_retained is None, never ZeroDivisionError
    fires = [_lf(1, "shouldnt", activation="soft", score=0.8)]
    assert threshold_sweep(fires, [0.6])[0].soft_recall_retained is None


def test_threshold_sweep_soft_recall_fraction():
    fires = [_lf(1, "should", activation="soft", score=0.62),
             _lf(2, "should", activation="soft", score=0.80)]
    pts = {p.t: p for p in threshold_sweep(fires, [0.6, 0.7])}
    assert pts[0.6].soft_recall_retained == pytest.approx(1.0)
    assert pts[0.7].soft_recall_retained == pytest.approx(0.5)      # only the 0.80 survives


# ── report ───────────────────────────────────────────────────────────────────────────

def _counts(should=0, shouldnt=0, skip=0, total=0):
    by = {k: v for k, v in (("should", should), ("shouldnt", shouldnt), ("skip", skip)) if v}
    labeled = should + shouldnt + skip
    return {"total": total or labeled, "labeled": labeled,
            "unlabeled": (total or labeled) - labeled, "by_signal": by}


def test_report_zero_labels_graceful():
    out = format_report([], _counts(total=352), config_version="0.2.0-taxonomy")
    assert "0 labeled" in out and "0.2.0-taxonomy" in out


def test_report_skip_rate_warning():
    out = format_report([_lf(1, "should")], _counts(should=1, skip=9), config_version="v")
    assert "skip_rate" in out and "⚠" in out           # 9/10 skipped -> warn


def test_report_renders_metrics_and_no_transcript_text():
    fires = [_lf(1, "should", causal=("multi_speaker",), inert=("topic_continuation",)),
             _lf(2, "shouldnt", causal=("multi_speaker",))]
    out = format_report(fires, _counts(should=1, shouldnt=1), config_version="v")
    assert "multi_speaker" in out and "topic_continuation" in out   # names, not text
    assert "co-occurring INERT" in out and "clarity band" in out and "threshold sweep" in out


# ── load_labeled (seeded read-only DB integration) ──────────────────────────────────

def _row_tuple(id_, config_version, *, activation="soft", score=0.65, clarity=0.9, signal=None,
               triggers=(("multi_speaker", "soft", 0.4),), utt_ids=(1, 2, 3)):
    """A full attention_events tuple in crud.COLUMNS order (bulk_upsert writes labels on insert)."""
    triggers_json = json.dumps([{"name": n, "kind": k, "contribution": c} for n, k, c in triggers])
    window_ref = json.dumps({"snapshot_id": "s", "session_id": "s1", "utt_ids": list(utt_ids),
                             "ts_start": 0.0, "ts_end": 1.0})
    ts = "2026-07-01T00:00:00+00:00"
    return (id_, ts, "s1", activation, score, triggers_json, json.dumps([]), window_ref,
            "unknown", clarity, None, signal, "s", config_version, ts)


async def _seed(path, rows):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute(TABLES["attention_events"])
    for idx in INDEXES:
        if "attention_events" in idx:
            await conn.execute(idx)
    await conn.commit()
    await crud.bulk_upsert_events(conn, rows)
    await conn.close()


@pytest.mark.asyncio
async def test_load_labeled_scopes_version_and_excludes_skip_unlabeled(tmp_path):
    p = tmp_path / "g.db"
    await _seed(p, [
        _row_tuple("a", "0.2.0-taxonomy", signal="should", utt_ids=(11,)),
        _row_tuple("b", "0.2.0-taxonomy", signal="shouldnt", utt_ids=(22,)),
        _row_tuple("c", "0.2.0-taxonomy", signal="skip", utt_ids=(33,)),        # excluded
        _row_tuple("d", "0.2.0-taxonomy", signal=None, utt_ids=(44,)),          # unlabeled excluded
        _row_tuple("e", "0.1.0-default", signal="should", utt_ids=(55,)),       # other version
    ])
    fires, unresolved, counts = await load_labeled(p, "0.2.0-taxonomy")
    assert {f.key for f in fires} == {11, 22}          # only labeled non-skip of THIS version
    assert unresolved == 0
    assert counts["by_signal"].get("skip") == 1 and counts["labeled"] == 3   # counts see skip too


@pytest.mark.asyncio
async def test_load_labeled_maps_causal_split_and_score(tmp_path):
    p = tmp_path / "g.db"
    await _seed(p, [
        _row_tuple("a", "0.2.0-taxonomy", signal="should", score=0.71, utt_ids=(11,),
                   triggers=(("question", "soft", 0.3), ("topic_continuation", "soft", 0.0),
                             ("multi_speaker", "soft", 0.4))),
    ])
    fires, _, _ = await load_labeled(p, "0.2.0-taxonomy")
    lf = fires[0]
    assert lf.causal_soft == frozenset({"question", "multi_speaker"})
    assert lf.inert_soft == frozenset({"topic_continuation"})
    assert lf.score == pytest.approx(0.71) and lf.signal == "should"
