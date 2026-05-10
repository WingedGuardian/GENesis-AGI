"""Versioned rubrics for the LLM-as-judge primitive.

A Rubric defines:
  - the prompt template the judge sees,
  - a pass threshold (judge score below this fails the case),
  - a version string (bump on substantive prompt edits so calibration
    history stays interpretable).

Each concrete rubric lives in its own submodule under this package and
self-registers via ``register_rubric()`` at import time. The ``__init__``
imports the first-party rubrics so they're available after a single
``import genesis.eval.rubrics``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Registry of name → Rubric. Populated by register_rubric() at submodule
# import time.
_RUBRICS: dict[str, Rubric] = {}


@dataclass(frozen=True)
class Rubric:
    """A versioned grading rubric for the LLM-as-judge primitive.

    Args:
        name: Stable identifier used in eval_results.scorer_detail and
            cost_events metadata. Must be unique across the registry.
        version: Semver-like string (e.g. "1.0.0"). Bump on any
            prompt_template change so calibration runs against an old
            version remain interpretable.
        description: Human-readable summary of what this rubric grades.
        prompt_template: Format string passed to the judge model. Must
            contain ``{actual}`` and ``{expected}`` placeholders. Other
            placeholders (e.g. ``{query}``) come from the EvalCase's
            ``scorer_config``.
        pass_threshold: Judge score in [0, 1] below which the case fails.
            Default 0.7 — tuned per rubric during calibration.
        extra_placeholders: Tuple of additional placeholder names the
            template expects from ``scorer_config``. Empty by default.
    """

    name: str
    version: str
    description: str
    prompt_template: str
    pass_threshold: float = 0.7
    extra_placeholders: tuple[str, ...] = ()


def register_rubric(rubric: Rubric) -> None:
    """Register a rubric in the global registry.

    Raises ValueError if name collides with an already-registered rubric
    of a different version — silent overwrite would mask copy-paste
    accidents and corrupt calibration history.
    """
    existing = _RUBRICS.get(rubric.name)
    if existing is not None and existing != rubric:
        msg = (
            f"Rubric name collision: {rubric.name!r} already registered "
            f"with version {existing.version}; refusing to overwrite "
            f"with version {rubric.version}. Rename the new rubric or "
            f"bump the version on the existing one."
        )
        raise ValueError(msg)
    _RUBRICS[rubric.name] = rubric


def get_rubric(name: str) -> Rubric:
    """Look up a registered rubric by name.

    Raises KeyError with a helpful message if not found — typo-catching
    matters when rubric names are referenced from dataset YAML.
    """
    rubric = _RUBRICS.get(name)
    if rubric is None:
        available = ", ".join(sorted(_RUBRICS)) or "(none)"
        msg = f"unknown rubric {name!r}; available: {available}"
        raise KeyError(msg)
    return rubric


def list_rubrics() -> list[Rubric]:
    """Return all registered rubrics, sorted by name. Mostly for tests."""
    return [_RUBRICS[k] for k in sorted(_RUBRICS)]


# Auto-import first-party rubrics so they self-register.
# New rubrics are added here.
from genesis.eval.rubrics import memory_recall_grounding  # noqa: E402,F401
