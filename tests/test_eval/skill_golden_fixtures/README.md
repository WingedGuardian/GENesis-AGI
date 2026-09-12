# Synthetic evaluation regression cases

`evaluate_fixture.jsonl` and `user_evaluate_fixture.jsonl` each contain six
invented research cases with frozen source dossiers, relevant Genesis/user
facts, and criteria written before replay. They cover distinctive mechanisms,
integration costs, justified adaptation/rejection, missing evidence, and
personal relevance. Adoption itself earns no credit. These public examples
contain no real inbox reports, conversations, or user history.

From the repository/worktree root, with the Genesis virtual environment
activated:

```bash
PYTHONPATH="$PWD/src" python -m pytest -q tests/test_eval/test_evaluation_golden_fixtures.py
```

The tests validate both suites and exercise the existing paired runner with
deterministic fake generation/scoring. Six complete tied pairs correctly yield
`inconclusive`. This validates fixture/runner compatibility; it does not measure
model judgment. Locking and live credentials are bypassed only in those tests.

For an optional real-model comparison, keep independently authored held-out
suites outside the repository, conventionally
`~/.genesis/eval/skill_golden/<skill>.jsonl`. Keep real private examples there;
never enable `allow_repo_path` or `allow_repo_tasks` for private data. Save the
baseline and proposed skill bodies beside the private suite as
`<skill>_old.md` and `<skill>_new.md`. With configured Claude Code and judge
credentials, this invokes the existing runner (model calls incur normal usage;
its temporary worktree is cleaned after the run):

```bash
PYTHONPATH="$PWD/src" python - <<'PY'
import asyncio
from pathlib import Path
from genesis.cc.types import CCModel, EffortLevel
from genesis.eval.bench.tasks import load_tasks
from genesis.eval.skill_replay.runner import run_skill_replay
from genesis.eval.skill_replay.types import SkillReplayConfig

skill = "evaluate"  # Repeat with "user_evaluate" and its own private files.
root = Path.home() / ".genesis" / "eval" / "skill_golden"
tasks = root / f"{skill}.jsonl"
loaded, version, digest = load_tasks(tasks)
print(f"Validated {len(loaded)} cases: {version} {digest}")
report = asyncio.run(run_skill_replay(
    skill_name=skill,
    old_content=(root / f"{skill}_old.md").read_text(),
    new_content=(root / f"{skill}_new.md").read_text(),
    tasks_path=tasks,
    model=CCModel.SONNET,
    effort=EffortLevel.MEDIUM,
    config=SkillReplayConfig(min_pairs=6),
))
print(report.verdict)
print(report.notes)
print(report.prod_delta)
for pair in report.pairs:
    print(f"\n## {pair.task.id}\nOLD:\n{pair.old.output_text}\nNEW:\n{pair.new.output_text}")
PY
```

The generic `bench_task_success` scalar judge is not calibrated to these
criteria. Human inspection of both outputs remains necessary; infrastructure
skips or too few pairs mean insufficient evidence. Fix model/effort, source
facts, and criteria across comparisons and preserve reported suite hashes.
Do not tune against these public cases and call them held-out evidence.

Replay pins one skill into a bare isolated invocation. It does not recreate
the live inbox's composed prompt, skill lookup, source fetching, memory,
approvals, or report persistence. Frozen dossiers avoid needing those systems
in these component cases. Future inbox prompt changes need separate composed
prompt and live workflow verification; concatenating skill files does not by
itself reproduce production behavior.
