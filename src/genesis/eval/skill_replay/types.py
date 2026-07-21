"""Types for the skill-replay held-out regression gate (WS1).

A proposed SKILL.md edit is screened by REPLAYING a frozen golden task suite
against the OLD vs NEW skill content and comparing per-task judge scores —
control = OLD, treatment = NEW. The verdict is recommend-only (it is logged as
an observation and NEVER blocks an edit); the promotion posture it encodes is
the spec's "promote only on zero-regression + net-positive". See ``verdict.py``.

Reuses the bench's pure task/outcome dataclasses (``BenchTask``,
``BenchArmOutcome`` — no CC, no DB) so the golden-suite format and the arm
skip-semantics are shared across eval harnesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genesis.eval.bench.types import BenchArmOutcome, BenchTask

# Verdict labels — the three outcomes of a replay comparison.
VERDICT_NET_POSITIVE = "net_positive"  # zero regression AND at least one improvement
VERDICT_REGRESSION = "regression"  # NEW lost on >=1 task, or pass-rate favours OLD
VERDICT_INCONCLUSIVE = "inconclusive"  # all ties, or too few complete pairs to judge

# Arm identifiers for the paired replay — control = OLD (current) skill content,
# treatment = NEW (proposed). Plain strings so the dependency-light modules
# (runner, persist) can label without importing the CC arm builder.
ARM_OLD = "old"
ARM_NEW = "new"


@dataclass(frozen=True)
class SkillReplayConfig:
    """Statistical knobs for the verdict (operator-tunable via skill_gate_config).

    ``epsilon`` is the per-task judge-score margin a win must exceed
    (``compute_score_winrate``); ``min_pairs`` is the minimum number of complete
    (non-skipped) task pairs required before a verdict is anything but
    ``inconclusive`` — below it the run has no power and says so honestly.
    """

    epsilon: float = 0.05
    min_pairs: int = 5


@dataclass(frozen=True)
class SkillReplayVerdict:
    """The recommend-only outcome of one OLD-vs-NEW replay comparison."""

    verdict: str  # one of VERDICT_*
    n_complete: int  # complete (non-skipped) pairs graded
    n_regressions: int  # tasks where OLD beat NEW by > epsilon
    n_improvements: int  # tasks where NEW beat OLD by > epsilon
    score_winrate: dict = field(default_factory=dict)  # compute_score_winrate output
    pass_winrate: dict = field(default_factory=dict)  # compute_winrate output
    note: str = ""


@dataclass(frozen=True)
class SkillReplayPair:
    """Both arms' outcomes on one golden task. control = OLD, treatment = NEW."""

    task: BenchTask
    old: BenchArmOutcome
    new: BenchArmOutcome

    @property
    def skipped(self) -> bool:
        return self.old.skipped or self.new.skipped


@dataclass
class SkillReplayReport:
    """Aggregate result of one skill-replay run (the runner's return value)."""

    run_id: str
    skill_name: str
    model: str
    effort: str
    task_set_version: str
    task_file_sha256: str
    rubric_name: str
    rubric_version: str
    pairs: list[SkillReplayPair] = field(default_factory=list)
    verdict: SkillReplayVerdict | None = None
    prod_delta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    control_run_id: str = ""
    treatment_run_id: str = ""
