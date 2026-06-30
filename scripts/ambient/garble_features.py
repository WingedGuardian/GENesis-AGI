"""Pure feature functions for ambient STT analysis — NO I/O, NO LLM, NO genesis
imports, so they're unit-testable in isolation and port to the edge as plain
arithmetic.

The headline (and only wired-toward) deliverable is **capture-clarity**
(``capture_clarity`` / ``is_blip``, bottom of file): a signal-richness prioritizer
for the future attention engine — see the note above those functions.

> History: this module began as the maths for a "filter out hallucinated garble"
> approach. That framing was **corrected by user ground truth (2026-06-29): the
> ambient stream is ALL real household audio, just mis-transcribed** — there is no
> "fake garble"/TV class to filter. The garble-detection helpers below
> (``dict_coverage``, ``gate_metrics``, ``best_threshold``, ``stratified_sample``)
> are retained as general, tested utilities, but the *deliverable* is the
> capture-clarity score. See [[project_ambient_stt_garble]].
"""
from __future__ import annotations

import random
import re
from collections.abc import Callable, Hashable, Sequence
from typing import Any

# Token = a whitespace chunk reduced to its lowercase alphabetic core. Keeps an
# internal apostrophe ("don't") so contractions can be matched against vocab.
_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


# ── confidence statistics over per-token ys_log_probs ────────────────────────

def frac_below(values: Sequence[float], thr: float) -> float:
    """Fraction of values strictly < ``thr``. Empty → 0.0 (no evidence).

    ``frac_below(ys_log_probs, -1.0)`` is the lead garble statistic: real speech
    sits ~0%, garble 12-20% (measured on the live corpus 2026-06-29).
    """
    if not values:
        return 0.0
    return sum(1 for v in values if v < thr) / len(values)


def mean_or(values: Sequence[float], *, default: float) -> float:
    """Mean, or ``default`` when empty (no div-by-zero)."""
    return sum(values) / len(values) if values else default


def min_or(values: Sequence[float], *, default: float) -> float:
    """Min, or ``default`` when empty."""
    return min(values) if values else default


# ── lexical signals over the transcript text ─────────────────────────────────

def _word_tokens(text: str) -> list[str]:
    """Lowercase alphabetic word-cores from free text (drops digits/punctuation)."""
    return _WORD_RE.findall((text or "").lower())


def dict_coverage(text: str, vocab: set[str]) -> float:
    """Fraction of word-tokens present in ``vocab`` (real-English coverage).

    Catches class-(a) garble like "INSTITUAL"/"DOLLIONS"/"COMFENCE" which are
    fluent-looking but not real words. No word-tokens at all → 0.0 (no evidence
    it is real speech). ``vocab`` is injected so this stays pure/testable; the
    CLI builds the real vocab from nltk ``words`` + a casual-speech allowlist.
    """
    toks = _word_tokens(text)
    if not toks:
        return 0.0
    covered = sum(1 for t in toks if t in vocab or t.split("'", 1)[0] in vocab)
    return covered / len(toks)


def repetition_ratio(tokens: Sequence[str]) -> float:
    """1 - unique/total over tokens (0.0 for <2 tokens). ASR loops repeat tokens."""
    n = len(tokens)
    if n < 2:
        return 0.0
    return 1.0 - len(set(tokens)) / n


def max_repeat_run(tokens: Sequence[str]) -> int:
    """Longest run of consecutive identical tokens (0 for empty, 1 for all-distinct)."""
    best = run = 0
    prev = object()
    for t in tokens:
        run = run + 1 if t == prev else 1
        best = max(best, run)
        prev = t
    return best


# ── deterministic, proportional stratified sample (the judge set) ────────────

def stratified_sample(
    items: Sequence[Any],
    *,
    key_fn: Callable[[Any], Hashable],
    n: int,
    seed: int,
) -> list[Any]:
    """Pick ``n`` items spread proportionally across strata (largest-remainder),
    deterministically given ``seed``. ``n >= len(items)`` returns all items.

    Used to bound how many real household fragments leave to the external judge
    while keeping every (is_user × confidence × length) cell represented.
    """
    items = list(items)
    if n >= len(items):
        return items

    strata: dict[Hashable, list[Any]] = {}
    for it in items:
        strata.setdefault(key_fn(it), []).append(it)

    total = len(items)
    # Largest-remainder allocation so the picks sum to exactly n.
    raw = {k: n * len(v) / total for k, v in strata.items()}
    alloc = {k: int(v) for k, v in raw.items()}
    short = n - sum(alloc.values())
    # Distribute the remaining slots to the largest fractional remainders;
    # ties break by stratum key order for determinism.
    order = sorted(strata.keys(), key=lambda k: (-(raw[k] - alloc[k]), _sortable(k)))
    for k in order[:short]:
        alloc[k] += 1

    rng = random.Random(seed)
    out: list[Any] = []
    for k in sorted(strata.keys(), key=_sortable):
        bucket = strata[k]
        take = min(alloc.get(k, 0), len(bucket))
        out.extend(rng.sample(bucket, take))
    return out


def _sortable(k: Hashable) -> str:
    """Stable string key for deterministic ordering of heterogeneous stratum keys."""
    return repr(k)


# ── the tradeoff metric every curve is computed from ─────────────────────────

def gate_metrics(labels: Sequence[str], keep: Sequence[bool]) -> dict[str, Any]:
    """Score a keep/drop decision against real|garble ground truth.

    Non-{real,garble} labels (e.g. ``abstain``) are excluded from both
    denominators. Returns garble-killed (recall on garble) and real-dropped
    (the cost) plus raw counts. Empty denominators → 0.0, never a div-by-zero.
    """
    real_total = real_dropped = garble_total = garble_killed = 0
    for lab, k in zip(labels, keep, strict=False):
        if lab == "real":
            real_total += 1
            if not k:
                real_dropped += 1
        elif lab == "garble":
            garble_total += 1
            if not k:
                garble_killed += 1
    return {
        "real_total": real_total,
        "garble_total": garble_total,
        "real_dropped_n": real_dropped,
        "garble_killed_n": garble_killed,
        "real_dropped": (real_dropped / real_total) if real_total else 0.0,
        "garble_killed": (garble_killed / garble_total) if garble_total else 0.0,
    }


# ── greedy threshold search (depth-1; the building block of the stump) ────────

def _keep_mask(scores: Sequence[float], thr: float, direction: str) -> list[bool]:
    """direction='drop_high' → drop (keep=False) rows with score >= thr."""
    if direction == "drop_high":
        return [s < thr for s in scores]
    if direction == "drop_low":
        return [s > thr for s in scores]
    raise ValueError(f"unknown direction {direction!r}")


def best_threshold(
    scores: Sequence[float],
    labels: Sequence[str],
    *,
    max_real_dropped: float,
    direction: str = "drop_high",
) -> dict[str, Any] | None:
    """Best single threshold on ``scores`` maximizing garble-killed subject to
    real-dropped ≤ ``max_real_dropped``. Returns the winning metrics (with the
    chosen ``threshold``/``direction``), or ``None`` if no split kills any garble
    within budget. Candidate thresholds sit just above each observed score so a
    ``>=`` drop is exact.
    """
    uniq = sorted(set(scores))
    # Candidate cut points: above the max (drop nothing) and just above each value.
    eps = 1e-9
    candidates = [uniq[-1] + 1.0] + [v + eps for v in uniq]
    best: dict[str, Any] | None = None
    for thr in candidates:
        m = gate_metrics(labels, _keep_mask(scores, thr, direction))
        if m["real_dropped"] > max_real_dropped + 1e-12:
            continue
        if best is None or m["garble_killed"] > best["garble_killed"]:
            best = {**m, "threshold": thr, "direction": direction}
    if best is None or best["garble_killed"] == 0.0:
        return best if best and best["garble_killed"] > 0.0 else None
    return best


# ── capture-clarity: a heuristic signal-richness prioritizer ─────────────────
# NOT an accuracy measure and NOT a relevance measure. It orders utterances from
# near-silence one-word blips → loud, long, confident passages — "how clearly was
# this captured," which is all the stored data can honestly support. The original
# "filter out hallucinated garble" framing was WRONG (user ground truth 2026-06-29:
# the stream is ALL real household audio, just mis-transcribed); a dict-coverage
# accuracy proxy came out flat (~0.90 everywhere — single mis-heard real words score
# 1.0), and the raw audio isn't stored, so transcription ACCURACY is unverifiable.
# What survives is this clarity ordering, validated by human eyeball.
# Reference points from the live corpus (2026-06-29, n=6074): rms p05≈0.026 /
# p75≈0.168 · duration p75≈3.85s · n_tokens p75≈15 · frac_lt_1 p50≈0.083.

RMS_FLOOR = 0.02      # below ≈ near-silence (corpus p05≈0.026); the blip floor
RMS_REF = 0.17        # "loud / clearly captured" reference (corpus p75≈0.168)
DUR_REF = 4.0         # seconds for a "full-length" utterance (corpus p75≈3.85)
NTOK_REF = 8.0        # tokens for a "content-rich" utterance


def _clamp01(x: float) -> float:
    if x != x:  # NaN (the only value not equal to itself) — corrupt meta → worst-case
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def is_blip(rms: float, duration_s: float, n_tokens: int) -> bool:
    """True for a near-silence throwaway — the ASR emitting a word or two from
    almost no audio. Pure physics: ``rms`` below the near-silence floor AND sparse
    (sub-second OR ≤2 tokens). Deliberately conservative — flags ~1.8% of the live
    corpus, the unambiguous junk tail only. The one rock-solid signal here: it needs
    no accuracy or relevance judgement, just loudness."""
    return rms < RMS_FLOOR and (duration_s < 1.0 or n_tokens <= 2)


def capture_clarity(
    rms: float, duration_s: float, frac_lt_1: float, n_tokens: int,
) -> float:
    """Heuristic 0..1 capture-clarity score (see the module note: clarity, NOT
    accuracy, NOT relevance — relevance is ``is_user``, kept orthogonal). Equal-
    weight mean of four bounded sub-scores, so there's no false precision on weights
    (a consumer may reweight): ASR confidence (``1-frac_lt_1``), loudness, length,
    token-richness. Near-silence blips land near 0; loud/long/confident passages near
    1. Edge-portable — plain arithmetic, no population statistics."""
    confidence = 1.0 - _clamp01(frac_lt_1)
    loudness = _clamp01((rms - RMS_FLOOR) / (RMS_REF - RMS_FLOOR))
    length = _clamp01(duration_s / DUR_REF)
    richness = _clamp01(n_tokens / NTOK_REF)
    return (confidence + loudness + length + richness) / 4.0
