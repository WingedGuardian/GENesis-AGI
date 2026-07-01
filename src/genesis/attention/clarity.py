"""capture-clarity: a pure signal-richness prioritizer for the attention engine.

Runtime copy of the edge-portable arithmetic that also lives in
``scripts/ambient/garble_features.py`` (kept as a separate copy DELIBERATELY: the
genesis package cannot import ``scripts/`` — no ``__init__`` — and this math is
destined to be vendored to the edge voice repo unchanged; three deployment targets
that cannot import one another, so ~25 lines of stable arithmetic legitimately lives
in each). NO genesis imports, NO I/O.

It scores capture-clarity / signal-richness — near-silence one-word blip -> ~0, a
loud/long/confident passage -> ~1. Explicitly NOT accuracy and NOT relevance
(``is_user`` stays orthogonal). The attention engine weights an utterance's soft-score
contribution by its clarity (tolerate garbled text; don't require clean ASR) and drops
``is_blip`` near-silence junk from the context window. Reference points baked into the
constants are from the live corpus (2026-06-29, n=6074): rms p05~0.026 / p75~0.168 ·
duration p75~3.85s · n_tokens p75~15 · frac_lt_1 p50~0.083.
"""
from __future__ import annotations

from collections.abc import Sequence

RMS_FLOOR = 0.02   # below ~ near-silence (corpus p05~0.026); the blip floor
RMS_REF = 0.17     # "loud / clearly captured" reference (corpus p75~0.168)
DUR_REF = 4.0      # seconds for a "full-length" utterance (corpus p75~3.85)
NTOK_REF = 8.0     # tokens for a "content-rich" utterance


def frac_below(values: Sequence[float], thr: float) -> float:
    """Fraction of ``values`` strictly < ``thr``. Empty -> 0.0 (no evidence).

    ``frac_below(ys_log_probs, -1.0)`` is the lead ASR-confidence statistic: real
    speech sits ~0%, garble 12-20% (measured on the live corpus)."""
    if not values:
        return 0.0
    return sum(1 for v in values if v < thr) / len(values)


def _clamp01(x: float) -> float:
    if x != x:  # NaN (the only value != itself) — corrupt meta -> worst-case
        return 0.0
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def is_blip(rms: float, duration_s: float, n_tokens: int) -> bool:
    """True for a near-silence throwaway (the ASR emitting a word or two from almost
    no audio): ``rms`` below the near-silence floor AND sparse (sub-second OR <=2
    tokens). Conservative physics gate — flags ~1.8% of the live corpus, the
    unambiguous junk tail only. Needs no accuracy/relevance judgement, just loudness."""
    return rms < RMS_FLOOR and (duration_s < 1.0 or n_tokens <= 2)


def capture_clarity(
    rms: float, duration_s: float, frac_lt_1: float, n_tokens: int,
) -> float:
    """Heuristic 0..1 capture-clarity score — equal-weight mean of four bounded
    sub-scores (ASR confidence ``1-frac_lt_1``, loudness, length, token-richness).
    Near-silence blips land near 0; loud/long/confident passages near 1. Edge-portable
    plain arithmetic; NaN-safe (corrupt meta -> conservative low clarity)."""
    confidence = 1.0 - _clamp01(frac_lt_1)
    loudness = _clamp01((rms - RMS_FLOOR) / (RMS_REF - RMS_FLOOR))
    length = _clamp01(duration_s / DUR_REF)
    richness = _clamp01(n_tokens / NTOK_REF)
    return (confidence + loudness + length + richness) / 4.0
