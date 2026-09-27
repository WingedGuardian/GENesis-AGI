"""Typed decision primitives — the vocabulary of the System One tier.

A *decision* is an answer drawn from a declarable bounded set. That test is
structural, not a value judgment: it is satisfied by a hardcoded ``if/else``
standing in for a judgment just as much as by an LLM classification call.

Three primitives, following the shape every System One implementation has
converged on:

``CHOICE``
    One label from a caller-defined set, with probabilities per option.
``SCORE``
    Expected level on an ordered rubric, with a distribution.
``NOUL``
    A single probability that a statement is true.

**Primitive strength is not uniform, and the registry does not hide it.**
MEASURED upstream on a fine-tuned checkpoint: ``noul`` 0.857, ``choice`` 0.733,
``score`` 0.723 — and ``score`` is called out as the weakest primitive (0.372
on held-out ordinal data). Ranking work should decompose into repeated
``NOUL`` or pairwise ``CHOICE`` rather than reaching for ``SCORE``.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

#: Above this option count a ``CHOICE`` must declare a cardinality strategy.
#: Options share a fixed token budget in encoder-based decision models, so
#: labels stop being distinguishable as the set grows. MEASURED upstream:
#: Banking77 scored 0.425 at 77 options against 0.870 for a model without the
#: constraint, because each label received only ~3-4 tokens.
CARDINALITY_SOFT_CAP = 20


class QuestionType(StrEnum):
    CHOICE = "choice"
    SCORE = "score"
    NOUL = "noul"


class Consumes(StrEnum):
    """What the CALLER does with the answer — which decides what it needs.

    This is the load-bearing field of the whole registry. It turns "may this
    site branch on a probability?" from something a reviewer has to notice
    into a declared property a linter enforces.
    """

    #: Uses the top label only. Needs accuracy. Works without calibration.
    ARGMAX = "argmax"
    #: Uses relative order across items. Needs ranking. Works without calibration.
    ORDERING = "ordering"
    #: Branches on an absolute probability. **Requires calibration.**
    THRESHOLD = "threshold"


class Mode(StrEnum):
    """Resolved per call, from install state — never branched on by a site author."""

    #: Local model plus a calibration fitted for this site and adapter version.
    CALIBRATED = "calibrated"
    #: Typed and valid, confidence advisory only. No site may threshold here.
    TYPED = "typed"
    #: No decision backend at all. The site's pre-existing behaviour.
    LEGACY = "legacy"


class Verdict(StrEnum):
    """What a thresholding site does with one score."""

    ACT = "act"
    DECLINE = "decline"
    #: Inside the dead band (or not in Calibrated mode) — take ``fallback.typed``.
    ABSTAIN = "abstain"


#: Closed set for ``tie_rule``: whether a score exactly on a band EDGE is decided
#: (``inclusive``) or abstains (``exclusive``). Decision models commonly return
#: two-decimal probabilities, so exact edge hits are ordinary, not rare.
TIE_RULES = frozenset({"inclusive", "exclusive"})

#: Narrowest band accepted. Decision models report probabilities to two
#: decimals, so a band far below 0.01 cannot separate anything; and edges are
#: rounded to 9 places, so a band near that resolution would collapse onto the
#: cut and break "a score on the cut is always inside the band".
MIN_DEAD_BAND = 0.001


def _band_edges(threshold: float, band: float) -> tuple[float, float]:
    """Upper and lower band edges, rounded so exact two-decimal hits stay exact.

    Unrounded, 0.85 + 0.07 is 0.9199999999999999: a score of exactly 0.92
    would land just outside the upper edge and change verdict under the
    exclusive tie rule. Across all two-decimal cut/band pairs, 1,499 give a
    wrong edge without this rounding.
    """
    return round(threshold + band, 9), round(threshold - band, 9)


def band_problem(threshold: object, band: object, tie_rule: object) -> str | None:
    """The one place a band is validated — used by the loader AND by gate().

    Returns a description of what is wrong, or None. Kept independent of the
    loader so a directly constructed spec cannot skip the rules.
    """
    for label, value in (("threshold", threshold), ("dead_band", band)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{label}={value!r} must be a number"
        try:
            finite = math.isfinite(value)
        except OverflowError:  # an int too large to be a float
            finite = False
        if not finite:
            return f"{label}={value!r} must be finite"
    if not (0.0 < threshold < 1.0):
        return f"threshold={threshold} must lie strictly inside (0, 1)"
    if not (band >= MIN_DEAD_BAND):
        return f"dead_band={band} must be at least {MIN_DEAD_BAND}"
    upper, lower = _band_edges(float(threshold), float(band))
    if not (lower > 0.0 and upper < 1.0):
        return (
            f"dead_band={band} around threshold={threshold} leaves (0, 1) — one "
            "side of the cut could never be decided"
        )
    if tie_rule not in TIE_RULES:
        return f"tie_rule={tie_rule!r} is not one of: {sorted(TIE_RULES)}"
    return None


@dataclass(frozen=True, slots=True)
class Fallback:
    """What a site does when it may not act on a probability. Both arms are mandatory.

    ``typed`` also covers a thresholding site's ABSTAIN verdict: a score inside
    the dead band gets the same behaviour as a site running uncalibrated.
    """

    typed: str
    legacy: str


@dataclass(frozen=True, slots=True)
class DecisionSpec:
    """One registered question. Immutable once loaded."""

    name: str
    type: QuestionType
    instructions: str
    consumes: Consumes
    fallback: Fallback
    owner: str
    options: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    criteria: tuple[str, ...] = ()
    threshold: float | None = None
    dead_band: float | None = None
    tie_rule: str | None = None
    latency_budget_ms: int | None = None
    outcome_source: str | None = None
    cardinality_strategy: str | None = None

    def gate(self, p: float, *, mode: Mode | str) -> Verdict:
        """Turn one score into ACT / DECLINE / ABSTAIN for a thresholding site.

        A score near its cut is not repeatable: re-sending the identical
        request to a pinned decision model flipped roughly one near-cut
        decision in five at a 0.50 cut, while the aggregate action rate barely
        moved because the flips cancel. So the cut is surrounded by a band and
        anything inside it abstains rather than taking a branch that a retry
        could reverse. A score exactly on the cut is always inside the band.

        ABSTAIN means "take ``fallback.typed``" — the site's behaviour when it
        may not branch on a probability. That is also what every score gets
        outside Calibrated mode, where no site may threshold, so ``mode`` is
        required rather than defaulted.

        ``p`` must be the answer's probability, never its ``value``: a bool is
        rejected so a yes/no answer cannot skip the band as 1.0 or 0.0.
        """
        if self.consumes != Consumes.THRESHOLD or self.threshold is None:
            raise ValueError(f"decision {self.name!r} does not threshold; gate() is undefined")
        problem = band_problem(self.threshold, self.dead_band, self.tie_rule)
        if problem is not None:
            raise ValueError(f"decision {self.name!r}: {problem}")
        if isinstance(p, bool) or not isinstance(p, numbers.Real):
            raise ValueError(f"score {p!r} is not a probability")
        p = float(p)
        if not (0.0 <= p <= 1.0):  # also rejects NaN, which fails every comparison
            raise ValueError(f"score {p!r} is not a probability")
        if not self.usable_in(mode):
            return Verdict.ABSTAIN
        upper, lower = _band_edges(self.threshold, self.dead_band)
        if self.tie_rule == "inclusive":
            if p >= upper:
                return Verdict.ACT
            if p <= lower:
                return Verdict.DECLINE
        else:
            if p > upper:
                return Verdict.ACT
            if p < lower:
                return Verdict.DECLINE
        return Verdict.ABSTAIN

    def __post_init__(self) -> None:
        """Defensively copy the mappings so "immutable once loaded" is true.

        ``MappingProxyType`` is a *view*: wrapping a caller's live dict leaves
        them holding a handle that mutates this frozen instance. The loader
        already builds fresh dicts, but direct construction is a public path.
        """
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))
        object.__setattr__(self, "criteria", tuple(self.criteria))

    @property
    def cardinality(self) -> int:
        """Size of the answer space. ``NOUL`` is binary by construction."""
        if self.type == QuestionType.CHOICE:
            return len(self.options)
        if self.type == QuestionType.SCORE:
            return len(self.criteria)
        return 2

    @property
    def requires_calibration(self) -> bool:
        """Only a thresholding consumer needs a calibrated probability.

        ``==`` not ``is``: these are ``StrEnum`` members, so a plain string
        that arrived from YAML, JSON or SQLite is *equal* to the member but
        never *identical* to it. An identity check here silently reports that
        a thresholding site needs no calibration — which is the precise
        failure ``consumes`` exists to prevent.
        """
        return self.consumes == Consumes.THRESHOLD

    def usable_in(self, mode: Mode | str) -> bool:
        """Whether this site can act on a decision in ``mode``.

        Degradation is per use class, not global: classification, routing and
        ranking sites are fully functional in Typed mode. Only a site that
        branches on an absolute probability is blocked there.

        ``mode`` is normalised rather than compared: an unrecognised mode
        raises ``ValueError`` instead of silently taking the permissive
        branch. Callers resolve mode from install state, which means it
        reaches here as a string more often than as a member.
        """
        resolved = Mode(str(mode))
        if resolved == Mode.CALIBRATED:
            return True
        if resolved == Mode.LEGACY:
            return False
        return not self.requires_calibration


@dataclass(frozen=True, slots=True)
class Decision:
    """A returned answer, carrying the provenance a gate needs to trust it.

    ``calibration_version`` is not bookkeeping. A stale calibration is worse
    than none because it still looks valid, so a gate whose threshold was
    fitted against a different version must degrade to advisory rather than
    silently branch on a number from a distribution it never saw.
    """

    name: str
    value: str | float | bool
    distribution: Mapping[str, float]
    confidence: float
    mode: Mode
    backend: str
    model_version: str | None = None
    calibration_version: str | None = None
