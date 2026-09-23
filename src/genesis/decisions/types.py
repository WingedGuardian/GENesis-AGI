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


@dataclass(frozen=True, slots=True)
class Fallback:
    """What a site does outside Calibrated mode. Both arms are mandatory."""

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
    latency_budget_ms: int | None = None
    outcome_source: str | None = None
    cardinality_strategy: str | None = None

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
