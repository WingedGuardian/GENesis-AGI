"""Dataclasses for the A/B bench harness."""

from __future__ import annotations

from dataclasses import dataclass, field

#: Task categories the v1 pilot supports. ``multi_session`` is deliberately
#: absent — pilot scope (user-decided 2026-07-09); add it when the harness
#: grows session-resume plumbing.
VALID_CATEGORIES: frozenset[str] = frozenset({"research", "drafting", "recall"})

#: Arm identifiers. "bare" is the control, "genesis" the treatment —
#: mirrored in eval_runs.model_profile as "bench:bare"/"bench:genesis".
ARM_BARE = "bare"
ARM_GENESIS = "genesis"

#: Default per-task CC session timeout. Mirrors the gauntlet's 1200s
#: (gauntlet.py meta default). Justified deviation from the 7200s project
#: floor: a hung arm serially blocks the entire paired run, and a timeout is
#: scored as an infra SKIP for the whole pair — never a quality FAIL — so a
#: short cap can only shrink N, never corrupt the comparison. Override
#: per-task in the JSONL for legitimately long tasks.
DEFAULT_TASK_TIMEOUT_S = 1200


@dataclass(frozen=True)
class BenchTask:
    """One replayable task, loaded from the private JSONL.

    ``expected`` holds the EX-ANTE success criteria — written before any arm
    runs, frozen by the task file's sha256 recorded into run metadata. The
    judge grades ONLY against these.
    """

    id: str
    category: str
    prompt: str
    expected: str
    context: str = ""
    timeout_s: int = DEFAULT_TASK_TIMEOUT_S

    def rendered_prompt(self) -> str:
        """The exact prompt both arms receive (identical by construction)."""
        if self.context:
            return f"{self.prompt}\n\n<context>\n{self.context}\n</context>"
        return self.prompt


@dataclass(frozen=True)
class BenchArmOutcome:
    """One arm's result on one task."""

    task_id: str
    arm: str  # ARM_BARE | ARM_GENESIS
    output_text: str
    duration_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    # Judge verdict (filled by the scoring stage).
    judge_passed: bool = False
    judge_score: float = 0.0
    judge_detail: str = ""  # LLMJudgeScorer detail JSON (rubric_version etc.)
    # Infra failure (CC error/timeout or judge call/parse sentinel). A skipped
    # arm skips the WHOLE pair — paired stats need complete pairs.
    skipped: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class BenchPair:
    """Both arms' outcomes on one task."""

    task: BenchTask
    bare: BenchArmOutcome
    genesis: BenchArmOutcome

    @property
    def skipped(self) -> bool:
        return self.bare.skipped or self.genesis.skipped


@dataclass
class BenchReport:
    """Aggregate result of one bench run (also serialized to JSON)."""

    run_id: str
    model: str
    effort: str
    task_set_version: str
    task_file_sha256: str
    rubric_name: str
    rubric_version: str
    judge_calibrated: bool  # False until the bench golden set calibrates
    pairs: list[BenchPair] = field(default_factory=list)
    score_winrate: dict = field(default_factory=dict)
    pass_winrate: dict = field(default_factory=dict)
    control_run_id: str = ""
    treatment_run_id: str = ""
    prod_delta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
