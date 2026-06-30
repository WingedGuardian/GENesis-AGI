"""Unit tests for the ambient garble-analysis pure feature/metric functions.

These cover the genuinely-custom, correctness-critical logic the gate decision
rests on: the per-token confidence statistic, dict-coverage, repetition, the
deterministic stratified sample we send to the (paid-attention) LLM judge, the
tradeoff metric every curve is computed from, and the greedy stump that becomes
the edge gate. All pure — no I/O, no LLM, no genesis imports — so they pin the
maths independent of the snapshot/judge plumbing.
"""
import sys
from pathlib import Path

# Import the script module the way tests/test_hooks/* import scripts (sys.path).
_AMBIENT = Path(__file__).resolve().parents[2] / "scripts" / "ambient"
sys.path.insert(0, str(_AMBIENT))

import garble_features as gf  # noqa: E402

# ── frac_below: the lead confidence statistic frac(ys_log_probs < thr) ──

def test_frac_below_basic():
    assert gf.frac_below([-0.2, -0.3, -0.1], -1.0) == 0.0
    assert gf.frac_below([-2.0, -0.1], -1.0) == 0.5
    assert gf.frac_below([-2.0, -1.5, -3.0], -1.0) == 1.0


def test_frac_below_empty_is_zero():
    # No tokens → no evidence of garble (capture-side already dropped empties).
    assert gf.frac_below([], -1.0) == 0.0


def test_frac_below_boundary_is_strict_less_than():
    # exactly == thr must NOT count (strict <), matching the plan's frac(<-1).
    assert gf.frac_below([-1.0, -1.0], -1.0) == 0.0


def test_mean_or_and_min_or_handle_empty():
    assert gf.mean_or([-1.0, -3.0], default=0.0) == -2.0
    assert gf.mean_or([], default=0.0) == 0.0
    assert gf.min_or([-0.2, -2.0], default=0.0) == -2.0
    assert gf.min_or([], default=0.0) == 0.0


# ── dict_coverage: fraction of word-tokens that are real English ──

def test_dict_coverage_all_real():
    vocab = {"hey", "turn", "off", "the", "light"}
    assert gf.dict_coverage("hey turn off the light", vocab) == 1.0


def test_dict_coverage_garble_words_uncovered():
    # "DOLLIONS"/"COMFENCE" are ASR inventions → only "the" is covered.
    vocab = {"the", "buy", "under"}
    cov = gf.dict_coverage("AND BUY COMFENCE UNDER DOLLIONS the", vocab)
    # tokens: and(no) buy(yes) comfence(no) under(yes) dollions(no) the(yes) -> 3/6
    assert abs(cov - 0.5) < 1e-9


def test_dict_coverage_strips_punctuation_and_casefolds():
    vocab = {"hey", "off"}
    assert gf.dict_coverage("Hey, OFF!", vocab) == 1.0


def test_dict_coverage_no_word_tokens_is_zero():
    # punctuation/digits only → no evidence it's real speech.
    assert gf.dict_coverage("?? 123 --", {"hey"}) == 0.0


# ── repetition: ASR loops emit repeated tokens ──

def test_repetition_ratio():
    assert gf.repetition_ratio(["a", "a", "a"]) == (1.0 - 1.0 / 3.0)
    assert gf.repetition_ratio(["a", "b", "c"]) == 0.0
    assert gf.repetition_ratio([]) == 0.0
    assert gf.repetition_ratio(["a"]) == 0.0


def test_max_repeat_run():
    assert gf.max_repeat_run(["a", "a", "b", "a"]) == 2
    assert gf.max_repeat_run(["a", "b", "c"]) == 1
    assert gf.max_repeat_run([]) == 0


# ── stratified_sample: the deterministic, proportional judge sample ──

def test_stratified_sample_is_deterministic():
    items = [{"id": i, "g": i % 3} for i in range(60)]
    a = gf.stratified_sample(items, key_fn=lambda x: x["g"], n=12, seed=7)
    b = gf.stratified_sample(items, key_fn=lambda x: x["g"], n=12, seed=7)
    assert [x["id"] for x in a] == [x["id"] for x in b]
    assert len(a) == 12


def test_stratified_sample_respects_strata_proportions():
    # two equal strata of 10; n=4 → exactly 2 per stratum (largest-remainder).
    items = [{"id": i, "g": "A"} for i in range(10)] + [{"id": 100 + i, "g": "B"} for i in range(10)]
    out = gf.stratified_sample(items, key_fn=lambda x: x["g"], n=4, seed=1)
    counts = {}
    for x in out:
        counts[x["g"]] = counts.get(x["g"], 0) + 1
    assert counts == {"A": 2, "B": 2}


def test_stratified_sample_n_ge_population_returns_all():
    items = [{"id": i, "g": i % 2} for i in range(8)]
    out = gf.stratified_sample(items, key_fn=lambda x: x["g"], n=100, seed=3)
    assert sorted(x["id"] for x in out) == list(range(8))


# ── gate_metrics: the tradeoff measurement every curve depends on ──

def test_gate_metrics_perfect_gate():
    labels = ["real", "garble", "garble", "real"]
    keep = [True, False, False, True]
    m = gf.gate_metrics(labels, keep)
    assert m["garble_killed"] == 1.0
    assert m["real_dropped"] == 0.0
    assert m["real_total"] == 2 and m["garble_total"] == 2


def test_gate_metrics_drops_some_real():
    labels = ["real", "real", "garble", "garble"]
    keep = [True, False, False, False]  # killed both garble but dropped 1 real
    m = gf.gate_metrics(labels, keep)
    assert m["garble_killed"] == 1.0
    assert m["real_dropped"] == 0.5


def test_gate_metrics_ignores_abstain_and_other_labels():
    labels = ["real", "garble", "abstain", "real"]
    keep = [True, False, True, True]
    m = gf.gate_metrics(labels, keep)
    # abstain row excluded from both denominators
    assert m["real_total"] == 2 and m["garble_total"] == 1
    assert m["garble_killed"] == 1.0 and m["real_dropped"] == 0.0


def test_gate_metrics_empty_denominators_are_zero_not_div0():
    m = gf.gate_metrics(["real", "real"], [True, True])
    assert m["garble_total"] == 0
    assert m["garble_killed"] == 0.0  # no garble to kill → 0, not a ZeroDivisionError


# ── best_threshold: depth-1 split honouring the real-dropped budget ──

def test_best_threshold_picks_max_garble_kill_within_budget():
    # scores: higher frac(<-1) = more garble. drop rows with score >= thr.
    scores = [0.0, 0.0, 0.3, 0.4, 0.5]
    labels = ["real", "real", "garble", "garble", "garble"]
    # budget 0 real-dropped → thr must sit above the real rows (0.0) and at/below 0.3
    res = gf.best_threshold(scores, labels, max_real_dropped=0.0, direction="drop_high")
    assert res is not None
    assert res["garble_killed"] == 1.0
    assert res["real_dropped"] == 0.0


def test_best_threshold_returns_none_when_budget_impossible_without_real_loss():
    # real and garble fully overlap at the same score → can't kill garble w/o dropping real
    scores = [0.5, 0.5]
    labels = ["real", "garble"]
    res = gf.best_threshold(scores, labels, max_real_dropped=0.0, direction="drop_high")
    # killing the garble (score 0.5) also drops the real (score 0.5) → over budget → no valid split
    assert res is None or res["garble_killed"] == 0.0


# ── is_blip: the rock-solid near-silence physics gate ────────────────────────

def test_is_blip_flags_near_silence_short():
    # rms≈0.01 (below the 0.02 floor) + short/one-word → throwaway blip
    assert gf.is_blip(rms=0.01, duration_s=0.5, n_tokens=1) is True
    assert gf.is_blip(rms=0.008, duration_s=2.0, n_tokens=1) is True  # near-silent + 1 token


def test_is_blip_false_for_loud_or_rich():
    assert gf.is_blip(rms=0.20, duration_s=20.0, n_tokens=30) is False
    # quiet but long AND wordy → NOT a blip (only near-silence + short/sparse qualifies)
    assert gf.is_blip(rms=0.01, duration_s=5.0, n_tokens=10) is False


def test_is_blip_floor_is_strict():
    # exactly at the floor is not below it → not a blip
    assert gf.is_blip(rms=gf.RMS_FLOOR, duration_s=0.5, n_tokens=1) is False


# ── capture_clarity: heuristic 0..1 prioritizer (NOT an accuracy claim) ───────

def test_capture_clarity_extremes():
    hi = gf.capture_clarity(rms=0.25, duration_s=22.0, frac_lt_1=0.0, n_tokens=30)
    lo = gf.capture_clarity(rms=0.005, duration_s=0.5, frac_lt_1=1.0, n_tokens=1)
    assert hi > 0.9
    assert lo < 0.1
    assert hi > lo


def test_capture_clarity_bounded_0_1():
    for args in [(0.0, 0.0, 1.0, 0), (999.0, 999.0, -5.0, 999), (0.17, 4.0, 0.0, 8)]:
        c = gf.capture_clarity(*args)
        assert 0.0 <= c <= 1.0


def test_capture_clarity_handles_nan_meta():
    # corrupt meta (NaN rms/duration) must NOT yield a NaN score — stays in [0,1].
    nan = float("nan")
    c = gf.capture_clarity(rms=nan, duration_s=nan, frac_lt_1=0.0, n_tokens=6)
    assert c == c  # not NaN
    assert 0.0 <= c <= 1.0


def test_capture_clarity_monotonic_in_each_signal():
    base = dict(rms=0.1, duration_s=2.0, frac_lt_1=0.3, n_tokens=6)
    # louder → clearer
    assert gf.capture_clarity(**{**base, "rms": 0.18}) > gf.capture_clarity(**{**base, "rms": 0.04})
    # more ASR-confident (lower frac_lt_1) → clearer
    assert gf.capture_clarity(**{**base, "frac_lt_1": 0.0}) > gf.capture_clarity(**{**base, "frac_lt_1": 0.8})
    # longer → clearer
    assert gf.capture_clarity(**{**base, "duration_s": 6.0}) > gf.capture_clarity(**{**base, "duration_s": 0.6})
    # more tokens → clearer
    assert gf.capture_clarity(**{**base, "n_tokens": 20}) > gf.capture_clarity(**{**base, "n_tokens": 1})


def test_capture_clarity_blip_scores_low():
    # a near-silence blip should land near the bottom of the scale
    assert gf.capture_clarity(rms=0.01, duration_s=0.5, frac_lt_1=1.0, n_tokens=1) < 0.15
