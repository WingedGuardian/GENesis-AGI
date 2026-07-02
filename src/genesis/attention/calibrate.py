"""calibrate: OFFLINE precision-first calibration report for the attention shadow corpus.

Genesis-side ADAPTER (imports ``genesis.db.crud`` + ``genesis.env`` + aiosqlite) — NOT one of
the edge-portable core modules and NOT imported by ``genesis.attention.__init__``, so
``tests/test_attention/test_edge_portability.py`` is unaffected.

Turns the dashboard should/shouldn't labels on ``attention_events`` FIRES into per-trigger /
per-activation PRECISION (+ 95% Wilson intervals + low-n flags), a soft-threshold sweep, a
clarity-band split, and suppressor veto-correctness counts.

PRECISION-FIRST: the store holds only windows that FIRED, so this measures false-positives
(over-firing). True RECALL (moments the gate missed entirely) is out of scope — those windows
were never emitted; measuring them needs a non-fire labeling surface (a deferred follow-on).

Firewall: emits ONLY trigger names, activations, floats, utt-ids and counts — NEVER ambient
transcript text (which lives+dies in ambient.db on the edge).

CLI: ``python -m genesis.attention.calibrate --config-version 0.2.0-taxonomy [--db PATH]``.

⚠ ``triggers_fired`` stores EVERY hit including ``contribution == 0.0`` ones (verified on live
rows 2026-07-02: ``topic_continuation`` co-occurs on 206/352 fires at weight 0; hard triggers
store ``contribution == 0.0``). So the row mapper SPLITS triggers into ``causal_soft``
(``kind == soft`` and ``contribution > 0`` — actually drove the score), ``hard_triggers``
(``kind == hard`` — the cause of a HARD fire), and ``inert_soft`` (``kind == soft`` and
``contribution == 0`` — co-occurring, non-driving). Only causal triggers are scored; inert
ones are reported separately so a zero-weight trigger can't earn a phantom precision.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from genesis.db.crud import attention as attention_crud
from genesis.env import genesis_db_path

_PERK = ("hard", "soft")  # activations that are a perk (vs "suppressed", a veto)
LOW_N = 10                # a precision/rate figure below this n is flagged noisy


# ── value objects ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LabeledFire:
    """One labeled fire: the join of a persisted ``attention_events`` row + its human label.

    ``causal_soft``/``hard_triggers``/``inert_soft`` split the stored ``triggers_fired`` by
    (kind, contribution) so precision is only ever attributed to triggers that DROVE the fire.
    """

    key: int                      # window_ref.utt_ids[-1] (trigger utterance)
    activation: str               # "hard" | "soft" | "suppressed"
    score: float
    clarity: float | None
    signal: str                   # "should" | "shouldnt"  (skip excluded upstream)
    causal_soft: frozenset[str]
    hard_triggers: frozenset[str]
    inert_soft: frozenset[str]
    suppressors: frozenset[str]

    @property
    def causal(self) -> frozenset[str]:
        """Triggers that caused this fire — soft with weight + hard summons. Never inert."""
        return self.causal_soft | self.hard_triggers


@dataclass(frozen=True)
class PrecisionStat:
    """should/shouldnt tally → precision + Wilson interval + low-n flag."""

    should: int
    shouldnt: int

    @property
    def n(self) -> int:
        return self.should + self.shouldnt

    @property
    def precision(self) -> float | None:
        return (self.should / self.n) if self.n else None

    @property
    def wilson(self) -> tuple[float, float] | None:
        return wilson_interval(self.should, self.n)

    @property
    def low_n(self) -> bool:
        return self.n < LOW_N


@dataclass(frozen=True)
class SuppressorStat:
    """SUPPRESSED-event tally. ``n_miss`` = label ``should`` = the suppressor WRONGLY vetoed a
    window Genesis should have perked on. ``n_correct_veto`` = label ``shouldnt`` = correct veto.
    ``correct_veto_rate`` is the suppressor's PRECISION — reported only at n >= LOW_N (else noise)."""

    n_miss: int
    n_correct_veto: int

    @property
    def n(self) -> int:
        return self.n_miss + self.n_correct_veto

    @property
    def correct_veto_rate(self) -> float | None:
        return (self.n_correct_veto / self.n) if self.n >= LOW_N else None

    @property
    def low_n(self) -> bool:
        return self.n < LOW_N


@dataclass(frozen=True)
class SweepPoint:
    """One soft-threshold ``t``: precision over retained SOFT alone (the signal ``t`` controls)
    and over hard+retained-soft (what actually ships), plus the fraction of good soft fires kept."""

    t: float
    soft: PrecisionStat            # over SOFT fires with score >= t
    combined: PrecisionStat        # hard (always) + SOFT with score >= t
    n_soft_retained: int
    soft_recall_retained: float | None   # should-soft@>=t / should-soft-total (None if no should-soft)


# ── pure statistics ──────────────────────────────────────────────────────────────────

def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials. None when n == 0.

    Wilson (not normal-approx) because n is small — a 2/2 must read as [0.34, 1.0], not "1.0"."""
    if n <= 0:
        return None
    phat = k / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


# ── row → LabeledFire ────────────────────────────────────────────────────────────────

def labeled_from_row(row: dict) -> LabeledFire | None:
    """Map a persisted (labeled) ``attention_events`` row → LabeledFire. None if no utt_ids.

    Splits ``triggers_fired`` by (kind, contribution): hard → ``hard_triggers``; soft with
    contribution > 0 → ``causal_soft``; soft with contribution == 0 → ``inert_soft``.

    Total by contract: returns None (an "unresolvable" row, counted not raised) for a row missing
    utt_ids OR any REQUIRED field (activation/score/acceptance_signal) — never a KeyError."""
    activation = row.get("activation")
    score = row.get("score")
    signal = row.get("acceptance_signal")
    wr = json.loads(row["window_ref"]) if row.get("window_ref") else {}
    utt_ids = wr.get("utt_ids") or []
    if not utt_ids or activation is None or score is None or signal is None:
        return None
    causal_soft: set[str] = set()
    hard: set[str] = set()
    inert: set[str] = set()
    for h in (json.loads(row["triggers_fired"]) if row.get("triggers_fired") else []):
        name = h.get("name")
        if not name:
            continue
        if h.get("kind") == "hard":
            hard.add(name)
        elif float(h.get("contribution") or 0.0) > 0.0:
            causal_soft.add(name)
        else:
            inert.add(name)
    suppressors = frozenset(json.loads(row["suppressors"]) if row.get("suppressors") else [])
    clarity = row.get("clarity")
    return LabeledFire(
        key=int(utt_ids[-1]),
        clarity=(None if clarity is None else float(clarity)),
        activation=activation,
        score=float(score),
        signal=signal,
        causal_soft=frozenset(causal_soft),
        hard_triggers=frozenset(hard),
        inert_soft=frozenset(inert),
        suppressors=suppressors,
    )


# ── metrics (pure; operate on list[LabeledFire]) ─────────────────────────────────────

def _perks(fires: list[LabeledFire]) -> list[LabeledFire]:
    return [f for f in fires if f.activation in _PERK]


def _tally(fires: list[LabeledFire], predicate) -> PrecisionStat:
    should = sum(1 for f in fires if predicate(f) and f.signal == "should")
    shouldnt = sum(1 for f in fires if predicate(f) and f.signal == "shouldnt")
    return PrecisionStat(should, shouldnt)


def overall_precision(fires: list[LabeledFire]) -> PrecisionStat:
    """Precision over all HARD+SOFT (perk) fires."""
    return _tally(_perks(fires), lambda f: True)


def precision_by_activation(fires: list[LabeledFire]) -> dict[str, PrecisionStat]:
    """Precision split by activation tier (hard vs soft)."""
    return {act: _tally(fires, lambda f, a=act: f.activation == a) for act in _PERK}


def precision_by_trigger(fires: list[LabeledFire]) -> dict[str, PrecisionStat]:
    """Per-CAUSAL-trigger precision (soft-with-weight ∪ hard). Inert co-triggers are excluded —
    see ``cooccurring_triggers``. A fire with k causal triggers counts toward all k (marginal
    precision — the counts intentionally sum to more than the fire count)."""
    perks = _perks(fires)
    names = sorted({n for f in perks for n in f.causal})
    return {name: _tally(perks, lambda f, nm=name: nm in f.causal) for name in names}


def cooccurring_triggers(fires: list[LabeledFire]) -> dict[str, int]:
    """Zero-weight soft triggers (contribution == 0) by how often they co-occur on a perk fire.
    Visibility only — these drove NO fire, so they get no precision (e.g. ``topic_continuation``)."""
    counts: dict[str, int] = {}
    for f in _perks(fires):
        for name in f.inert_soft:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def suppressor_stats(fires: list[LabeledFire]) -> dict[str, SuppressorStat]:
    """Per-suppressor veto correctness over SUPPRESSED events (miss vs correct-veto counts)."""
    suppressed = [f for f in fires if f.activation == "suppressed"]
    names = sorted({n for f in suppressed for n in f.suppressors})
    out: dict[str, SuppressorStat] = {}
    for name in names:
        miss = sum(1 for f in suppressed if name in f.suppressors and f.signal == "should")
        veto = sum(1 for f in suppressed if name in f.suppressors and f.signal == "shouldnt")
        out[name] = SuppressorStat(miss, veto)
    return out


# clarity bands tuned to the observed corpus (every stored fire has clarity >= ~0.6, because
# the engine pre-multiplies relevance by clarity; a NULL band is guarded but empty in practice).
DEFAULT_CLARITY_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.7, "[0.0,0.7)"),
    (0.7, 0.85, "[0.7,0.85)"),
    (0.85, 1.0001, "[0.85,1.0]"),
)


def precision_by_clarity_band(
    fires: list[LabeledFire], bands: tuple[tuple[float, float, str], ...] = DEFAULT_CLARITY_BANDS
) -> dict[str, PrecisionStat]:
    """Precision of perk fires bucketed by capture clarity — exposes garble-driven low precision.
    The fixed ``bands`` are ALWAYS returned (even at n == 0, precision None) so coverage is explicit.
    NULL clarity is never folded into the low band: it gets its own ``clarity=NULL`` bucket, which is
    emitted ONLY when such rows exist — its presence is itself the signal that some rows lack clarity."""
    perks = _perks(fires)
    out: dict[str, PrecisionStat] = {}
    null_fires = [f for f in perks if f.clarity is None]
    if null_fires:
        out["clarity=NULL"] = _tally(null_fires, lambda f: True)
    for lo, hi, label in bands:
        out[label] = _tally(
            perks, lambda f, lo=lo, hi=hi: f.clarity is not None and lo <= f.clarity < hi
        )
    return out


def threshold_sweep(fires: list[LabeledFire], ts_values: list[float]) -> list[SweepPoint]:
    """Soft-threshold sweep. Raising ``t`` only ever DROPS soft fires (score is a fixed stored
    REAL). Reports soft-only precision (what ``t`` governs), combined precision (hard + retained
    soft = what ships), and the retained fraction of should-labeled soft fires (recall vs the
    current soft fire-set — guarded to None when there are no should-labeled soft fires)."""
    perks = _perks(fires)
    hard = [f for f in perks if f.activation == "hard"]
    soft = [f for f in perks if f.activation == "soft"]
    should_soft_total = sum(1 for f in soft if f.signal == "should")
    points: list[SweepPoint] = []
    for t in ts_values:
        retained = [f for f in soft if f.score >= t]
        soft_stat = _tally(retained, lambda f: True)
        combined_stat = _tally(retained + hard, lambda f: True)
        kept_should = sum(1 for f in retained if f.signal == "should")
        recall = (kept_should / should_soft_total) if should_soft_total else None
        points.append(SweepPoint(t, soft_stat, combined_stat, len(retained), recall))
    return points


# ── report ───────────────────────────────────────────────────────────────────────────

def _fmt_stat(s: PrecisionStat) -> str:
    if s.n == 0:
        return "   —   (n=0)"
    lo, hi = s.wilson  # type: ignore[misc]
    flag = " LOW" if s.low_n else ""
    return f"{s.precision:5.2f} [{lo:.2f}-{hi:.2f}, n={s.n}{flag}]"


def format_report(
    fires: list[LabeledFire], counts: dict, *, config_version: str, unresolved: int = 0
) -> str:
    """Assemble the value-free text report. ``counts`` is ``crud.label_counts`` (total/labeled/
    unlabeled/by_signal). Renders a labeling-quality header even at 0 labels."""
    by_signal = counts.get("by_signal", {})
    n_should = by_signal.get("should", 0)
    n_shouldnt = by_signal.get("shouldnt", 0)
    n_skip = by_signal.get("skip", 0)
    labeled_judged = n_should + n_shouldnt
    skip_rate = (n_skip / (labeled_judged + n_skip)) if (labeled_judged + n_skip) else 0.0

    L: list[str] = []
    L.append("\n=== ATTENTION CALIBRATION — PRECISION REPORT ===")
    L.append(f"config_version: {config_version}")
    L.append(
        f"corpus: total={counts.get('total', 0)}  labeled={counts.get('labeled', 0)}  "
        f"unlabeled={counts.get('unlabeled', 0)}  (unresolved rows skipped: {unresolved})"
    )
    L.append(
        f"labels: should={n_should}  shouldnt={n_shouldnt}  skip={n_skip}  "
        f"| base-rate should:shouldnt = {n_should}:{n_shouldnt}  | skip_rate={skip_rate:.2f}"
    )
    if skip_rate > 0.3:
        L.append(
            "  ⚠ skip_rate > 0.30 — corpus may be too garbled to calibrate from; "
            "check the clarity distribution of skipped fires before trusting these numbers."
        )
    if labeled_judged == 0:
        L.append("\n0 labeled (should/shouldnt) fires — nothing to score yet. Label a batch in the "
                 "Attention tab (config_version filter) and re-run.\n")
        return "\n".join(L)

    L.append("\n-- overall precision (HARD+SOFT) --")
    L.append(f"  {_fmt_stat(overall_precision(fires))}")

    L.append("\n-- precision by activation --")
    for act, stat in precision_by_activation(fires).items():
        L.append(f"  {act:<10} {_fmt_stat(stat)}")

    L.append("\n-- precision by CAUSAL trigger (soft-with-weight ∪ hard; sorted by n) --")
    by_trig = precision_by_trigger(fires)
    for name, stat in sorted(by_trig.items(), key=lambda kv: (-kv[1].n, kv[0])):
        L.append(f"  {name:<20} {_fmt_stat(stat)}")

    cooc = cooccurring_triggers(fires)
    if cooc:
        L.append("\n-- co-occurring INERT triggers (weight 0 — drove NO fire, not scored) --")
        for name, c in cooc.items():
            L.append(f"  {name:<20} co-occurs on {c} perk fire(s)")

    L.append("\n-- precision by clarity band (exposes garble-driven fires) --")
    for label, stat in precision_by_clarity_band(fires).items():
        L.append(f"  {label:<14} {_fmt_stat(stat)}")

    supp = suppressor_stats(fires)
    if supp:
        L.append("\n-- suppressor veto correctness (SUPPRESSED events) --")
        for name, s in supp.items():
            rate = f"  correct_veto_rate={s.correct_veto_rate:.2f}" if s.correct_veto_rate is not None else ""
            flag = " LOW" if s.low_n else ""
            L.append(f"  {name:<20} miss(should)={s.n_miss}  correct_veto(shouldnt)={s.n_correct_veto}  "
                     f"n={s.n}{flag}{rate}")

    L.append("\n-- soft-threshold sweep (soft-only precision = the signal t controls) --")
    L.append(f"  {'t':>5}  {'soft_prec':>26}  {'combined_prec':>26}  {'n_soft':>7}  {'soft_recall':>11}")
    for p in threshold_sweep(fires, [0.60, 0.65, 0.70, 0.75, 0.80]):
        rec = f"{p.soft_recall_retained:.2f}" if p.soft_recall_retained is not None else "  —"
        L.append(f"  {p.t:>5.2f}  {_fmt_stat(p.soft):>26}  {_fmt_stat(p.combined):>26}  "
                 f"{p.n_soft_retained:>7}  {rec:>11}")
    L.append("")
    return "\n".join(L)


# ── IO adapter (own read-only connection) ────────────────────────────────────────────

async def load_labeled(
    db_path: str | Path, config_version: str
) -> tuple[list[LabeledFire], int, dict]:
    """Load labeled (should/shouldnt) fires + label_counts for a config_version from a READ-ONLY
    genesis.db connection. Returns (fires, n_unresolved, counts). Mirrors ``differ.load_from_db``."""
    uri = f"file:{Path(db_path).expanduser()}?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True)
    try:
        conn.row_factory = aiosqlite.Row
        rows = await attention_crud.load_labeled_fires(conn, config_version)
        counts = await attention_crud.label_counts(conn, config_version)
    finally:
        await conn.close()
    fires: list[LabeledFire] = []
    unresolved = 0
    for r in rows:
        lf = labeled_from_row(dict(r))
        if lf is None:
            unresolved += 1
        else:
            fires.append(lf)
    return fires, unresolved, counts


# ── CLI ──────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m genesis.attention.calibrate",
        description="Offline precision-first calibration report over the attention shadow corpus.",
    )
    p.add_argument("--config-version", default="0.2.0-taxonomy",
                   help="which shadow-run's labeled fires to score (default: 0.2.0-taxonomy)")
    p.add_argument("--db", default=None, help="genesis.db path (default: env genesis_db_path())")
    return p


async def _run(args: argparse.Namespace) -> None:
    db_path = args.db or str(genesis_db_path())
    fires, unresolved, counts = await load_labeled(db_path, args.config_version)
    print(format_report(fires, counts, config_version=args.config_version, unresolved=unresolved))


def main(argv: list[str] | None = None) -> None:
    asyncio.run(_run(_build_parser().parse_args(argv)))


if __name__ == "__main__":
    main()
