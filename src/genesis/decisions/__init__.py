"""System One decision tier — typed answers over bounded choices.

Peer of ``genesis.routing``: routing answers *which model generates this
text*; this package answers *what is the typed answer, and how sure are we*.
Both are cognitive infrastructure that memory, triage, ego and autonomy call
into.

Today this package is the **registry only** — the enumerable catalogue of
Genesis's bounded-choice decisions, and the validation that keeps those
declarations honest. Coverage is not yet complete: an enumeration is a
floor, not the full set. Backends and calibration land on top of it.
"""

from genesis.decisions.registry import (
    RegistryError,
    load_registry,
    load_registry_from_string,
)
from genesis.decisions.types import (
    CARDINALITY_SOFT_CAP,
    MIN_DEAD_BAND,
    TIE_RULES,
    Consumes,
    Decision,
    DecisionSpec,
    Fallback,
    Mode,
    QuestionType,
    Verdict,
    band_problem,
)

__all__ = [
    "CARDINALITY_SOFT_CAP",
    "MIN_DEAD_BAND",
    "TIE_RULES",
    "Consumes",
    "Decision",
    "DecisionSpec",
    "Fallback",
    "Mode",
    "QuestionType",
    "Verdict",
    "band_problem",
    "RegistryError",
    "load_registry",
    "load_registry_from_string",
]
