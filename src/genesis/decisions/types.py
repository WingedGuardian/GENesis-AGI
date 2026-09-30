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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

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
    #: Inside the dead band, uncalibrated, or Typed mode — take ``fallback.typed``.
    ABSTAIN = "abstain"
    #: Legacy mode: no decision backend at all — take ``fallback.legacy``.
    #: Distinct from ABSTAIN because the typed fallback may itself need the
    #: backend that Legacy mode says is absent.
    LEGACY = "legacy"


#: Closed set for ``tie_rule``: whether a score exactly on a band EDGE is decided
#: (``inclusive``) or abstains (``exclusive``). Decision models commonly return
#: two-decimal probabilities, so exact edge hits are ordinary, not rare.
TIE_RULES = frozenset({"inclusive", "exclusive"})

#: Narrowest band accepted. Decision models report probabilities to two
#: decimals, so a band far below 0.01 cannot separate anything; and edges are
#: rounded to 9 places, so a band near that resolution would collapse onto the
#: cut and break "a score on the cut is always inside the band".
MIN_DEAD_BAND = 0.001

#: Precision of a declared cut or band, and of the band edges derived from them.
MAX_DECIMALS = 9


def _band_edges(threshold: float, band: float) -> tuple[float, float]:
    """Upper and lower band edges, rounded so exact two-decimal hits stay exact.

    Unrounded, 0.85 + 0.07 is 0.9199999999999999: a score of exactly 0.92
    would land just outside the upper edge and change verdict under the
    exclusive tie rule. Across all two-decimal cut/band pairs, 1,499 give a
    wrong edge without this rounding.
    """
    return round(threshold + band, MAX_DECIMALS), round(threshold - band, MAX_DECIMALS)


def band_problem(threshold: object, band: object, tie_rule: object) -> str | None:
    """The one place a band is validated — by the spec's model validator AND by gate().

    Returns a description of what is wrong, or None. The type checks below are
    unreachable through construction (the model's field types already
    enforce them); they stay because ``gate()`` re-runs this on a spec whose
    fields could have been forced with ``object.__setattr__``.
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
    for label, value in (("threshold", threshold), ("dead_band", band)):
        if round(float(value), MAX_DECIMALS) != float(value):
            # Band edges are rounded to MAX_DECIMALS places so two-decimal
            # scores land exactly on them. A finer declared value would be
            # moved by that rounding, deciding scores inside the true band.
            return f"{label}={value!r} has more than {MAX_DECIMALS} decimal places"
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
    if not isinstance(tie_rule, str) or tie_rule not in TIE_RULES:  # a list is unhashable in `in`
        return f"tie_rule={tie_rule!r} is not one of: {sorted(TIE_RULES)}"
    return None


#: A registry identifier: decision names, option labels, fallback behaviours,
#: outcome sources. ASCII snake_case only. A grammar, not a blank check: a
#: leading space, a non-breaking space or a zero-width character each made a
#: key that looked right and could never be looked up.
Identifier = Annotated[StrictStr, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*$")]
#: An owner is a dotted path of identifiers (``memory.store``).
DottedIdentifier = Annotated[
    StrictStr, StringConstraints(pattern=r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
]
#: Prose: any non-blank string. Never a coerced bool, number or null.
Text = Annotated[StrictStr, StringConstraints(strip_whitespace=True, min_length=1)]

#: Validation for everything below is pydantic's, in STRICT field types.
#: Hand-written per-field checks were a denylist: three review rounds each
#: found another type that ``str()`` or ``int()`` quietly coerced, or another
#: path (loader, overlay, direct construction) weaker than the others. One
#: model now holds every rule, and loading and direct construction share it.
_CONFIG = ConfigDict(extra="forbid")

#: Calibration versions are opaque tokens, but printable ASCII ones: a blank
#: or zero-width string must not count as provenance.
_CALIBRATION_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")


@pydantic_dataclass(frozen=True, config=_CONFIG)
class Fallback:
    """What a site does when it may not act on a probability. Both arms are mandatory.

    ``typed`` also covers a thresholding site's ABSTAIN verdict: a score inside
    the dead band gets the same behaviour as a site running uncalibrated.
    """

    typed: Identifier
    legacy: Identifier


@pydantic_dataclass(frozen=True, config=_CONFIG)
class DecisionSpec:
    """One registered question. Immutable, and validated on construction.

    Loading and direct construction run the same rules; there is no path
    that skips them, and ``dataclasses.replace`` re-validates.

    Not hashable, picklable or deep-copyable: ``options`` is a read-only
    ``MappingProxyType``. Key caches on ``spec.name`` instead.
    """

    name: Identifier
    type: QuestionType
    instructions: Text
    consumes: Consumes
    fallback: Fallback
    owner: DottedIdentifier
    options: Mapping[Identifier, Text] = field(default_factory=lambda: MappingProxyType({}))
    criteria: tuple[Text, ...] = ()
    threshold: float | int | None = None
    dead_band: float | int | None = None
    tie_rule: Literal["inclusive", "exclusive"] | None = None
    latency_budget_ms: Annotated[StrictInt, Field(gt=0)] | None = None
    outcome_source: Identifier | None = None
    cardinality_strategy: Literal["shortlist", "hierarchical"] | None = None

    @field_validator("options", mode="after")
    @classmethod
    def _freeze_options(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        # A copy, then a read-only view: wrapping the caller's own dict would
        # leave them a handle that mutates this frozen instance.
        return MappingProxyType(dict(value))

    @field_validator("threshold", "dead_band", mode="before")
    @classmethod
    def _exact_number(cls, value: Any) -> Any:
        # Exact types, not "anything pydantic can turn into a float": a
        # Fraction or Decimal was silently converted, and a bool is an int.
        if value is not None and type(value) not in (int, float):
            raise ValueError(f"{value!r} must be a plain int or float")
        return value

    @field_validator("criteria", mode="before")
    @classmethod
    def _criteria_are_ordered(cls, value: Any) -> Any:
        # A string is a sequence of characters and a set has no order; both
        # were silently accepted as "criteria".
        if not isinstance(value, (list, tuple)):
            raise ValueError("criteria must be an ordered list of levels")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> DecisionSpec:
        qtype, consumes = self.type, self.consumes
        # Each primitive declares its own answer space and nothing else's.
        if qtype == QuestionType.CHOICE:
            if self.criteria:
                raise ValueError("a choice declares options, not criteria")
            if len(self.options) < 2:
                raise ValueError("a choice needs an options mapping with at least 2 entries")
        elif qtype == QuestionType.SCORE:
            if self.options:
                raise ValueError("a score declares criteria, not options")
            if len(self.criteria) < 2:
                raise ValueError("a score needs an ordered criteria list with at least 2 levels")
            if len(set(self.criteria)) != len(self.criteria):
                raise ValueError(f"criteria must be distinct levels; got {list(self.criteria)}")
        else:
            if self.options or self.criteria:
                raise ValueError("a noul is binary and must not declare options or criteria")
            if self.cardinality_strategy is not None:
                raise ValueError("a noul is binary; cardinality_strategy does not apply")

        # A thresholding site declares its cut, the band it abstains in, and
        # its edge tie rule: decisions near a cut flip on an identical retry.
        band = (self.threshold, self.dead_band, self.tie_rule)
        if consumes == Consumes.THRESHOLD:
            missing = [
                k
                for k, v in zip(("threshold", "dead_band", "tie_rule"), band, strict=True)
                if v is None
            ]
            if missing:
                raise ValueError(
                    f"consumes=threshold requires an explicit {missing[0]} — decisions near "
                    "a cut flip on an identical retry, so the site must declare its cut, "
                    "the band it abstains in, and its edge tie rule"
                )
            problem = band_problem(*band)
            if problem is not None:
                raise ValueError(problem)
        else:
            for key, value in zip(("threshold", "dead_band", "tie_rule"), band, strict=True):
                if value is not None:
                    raise ValueError(
                        f"{key}={value!r} is set but consumes={consumes.value} — a stray "
                        f"{key} means gating was expected but never declared"
                    )

        # The answer space shares a fixed token budget whatever the primitive.
        if self.cardinality > CARDINALITY_SOFT_CAP and self.cardinality_strategy is None:
            raise ValueError(
                f"cardinality {self.cardinality} exceeds the soft cap of "
                f"{CARDINALITY_SOFT_CAP}; declare a cardinality_strategy "
                "(hierarchical | shortlist) — labels stop being distinguishable "
                "as the answer space grows"
            )
        return self

    def gate(
        self, p: float | None, *, mode: Mode | str, calibration_version: str | None = None
    ) -> Verdict:
        """Turn one score into ACT / DECLINE / ABSTAIN / LEGACY for a thresholding site.

        A score near its cut is not repeatable: re-sending the identical
        request to a pinned decision model flipped roughly one near-cut
        decision in five at a 0.50 cut, while the aggregate action rate barely
        moved because the flips cancel. So the cut is surrounded by a band and
        anything inside it abstains rather than taking a branch that a retry
        could reverse. A score exactly on the cut is always inside the band.

        ABSTAIN means "take ``fallback.typed``" — the site's behaviour when it
        may not branch on a probability. That is also what every score gets in
        Typed mode, where no site may threshold, and in Calibrated mode when
        the caller cannot name the calibration the score came from: an
        unprovenanced number must not branch. LEGACY means "take
        ``fallback.legacy``" — there is no backend at all. ``mode`` is required
        rather than defaulted. Use :meth:`fallback_for` to resolve either.

        In Legacy mode ``p`` is not examined at all (pass ``None``).

        Only PRESENCE of ``calibration_version`` is enforced here: it must be a
        well-formed token (printable ASCII, no spaces or invisible characters).
        That check lives here, and only here: ``usable_in`` answers whether a mode
        permits thresholding at all and does not see provenance, so a site must
        branch through ``gate()``, never on ``usable_in`` alone. Whether it
        is the CURRENT calibration for this site needs the calibration store,
        which does not exist yet; that comparison belongs to it.

        ``p`` must be the answer's probability, never its ``value``: a bool is
        rejected so a yes/no answer cannot skip the band as 1.0 or 0.0.
        """
        if self.consumes != Consumes.THRESHOLD or self.threshold is None:
            raise ValueError(f"decision {self.name!r} does not threshold; gate() is undefined")
        problem = band_problem(self.threshold, self.dead_band, self.tie_rule)
        if problem is not None:
            raise ValueError(f"decision {self.name!r}: {problem}")
        resolved = Mode(str(mode))
        if resolved == Mode.LEGACY:
            # Before the score check: with no backend there IS no score, and a
            # caller must not have to invent one to learn which fallback to take.
            return Verdict.LEGACY
        if isinstance(p, bool) or not isinstance(p, numbers.Real):
            raise ValueError(f"score {p!r} is not a probability")
        try:
            p = float(p)
        except (OverflowError, ValueError, TypeError) as exc:  # huge int / Fraction
            raise ValueError(f"score {p!r} is not a probability") from exc
        if not (0.0 <= p <= 1.0):  # also rejects NaN, which fails every comparison
            raise ValueError(f"score {p!r} is not a probability")
        provenanced = isinstance(calibration_version, str) and bool(
            _CALIBRATION_VERSION.fullmatch(calibration_version)
        )
        if not self.usable_in(resolved) or not provenanced:
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

    def fallback_for(self, verdict: Verdict | str) -> str:
        """The declared behaviour a non-acting verdict routes to."""
        resolved = Verdict(str(verdict))
        if resolved == Verdict.ABSTAIN:
            return self.fallback.typed
        if resolved == Verdict.LEGACY:
            return self.fallback.legacy
        raise ValueError(f"verdict {resolved.value!r} is a decision, not a fallback")

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

        This answers "may this CLASS of site branch in this mode?" It does not
        see calibration provenance; a thresholding site branches through
        :meth:`gate`, which does.
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
    than none because it still looks valid. ``DecisionSpec.gate`` enforces the
    first half today: a Calibrated-mode score with no version abstains. The
    second half, refusing a version that is not the site's CURRENT one, needs
    the calibration store and lands with it.
    """

    name: str
    value: str | float | bool
    distribution: Mapping[str, float]
    confidence: float
    mode: Mode
    backend: str
    model_version: str | None = None
    calibration_version: str | None = None
