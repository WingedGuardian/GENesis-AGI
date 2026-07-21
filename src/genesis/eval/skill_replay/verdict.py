"""The skill-replay verdict — pure statistics over per-task judge results.

Given the COMPLETE (non-skipped) task pairs of an OLD-vs-NEW replay, decide
``net_positive`` / ``regression`` / ``inconclusive``. control = OLD, treatment =
NEW. The bar is the spec's "promote only on zero-regression + net-positive":

  * REGRESSION  — NEW scored worse than OLD on >=1 task (by more than epsilon),
    OR the binary pass-rate favours OLD. This is the safety signal.
  * NET_POSITIVE — zero regressions AND NEW strictly improved on >=1 task.
  * INCONCLUSIVE — everything else (all ties, or fewer than ``min_pairs``
    complete pairs to judge).

Significance (McNemar exact, carried in the winrate dicts) is ADVISORY at
shadow scale, NOT a gate: at 8-15 tasks a clean 5-0 improvement only reaches
p=0.0625, so requiring significance would perversely reject genuine
improvements. The verdict rests on zero-regression + strict-improvement; a
future ENFORCE flip may add a significance floor once suites grow past ~30.

Pure and CC-free — the runner extracts the per-case lists and calls this.
"""

from __future__ import annotations

from genesis.eval.skill_replay.types import (
    VERDICT_INCONCLUSIVE,
    VERDICT_NET_POSITIVE,
    VERDICT_REGRESSION,
    SkillReplayConfig,
    SkillReplayVerdict,
)
from genesis.eval.stats import compute_score_winrate, compute_winrate


def compute_verdict(
    *,
    old_scores: list[float],
    new_scores: list[float],
    old_pass: list[bool],
    new_pass: list[bool],
    config: SkillReplayConfig,
) -> SkillReplayVerdict:
    """Decide the replay verdict from the complete task pairs.

    All four lists are the COMPLETE (non-skipped) pairs in the SAME task order
    and MUST be equal length (the runner drops any pair where either arm was
    skipped for infra reasons). ``old_*`` is control, ``new_*`` is treatment.
    """
    n = len(new_scores)
    if not (len(old_scores) == len(old_pass) == len(new_pass) == n):
        msg = (
            f"per-case lists must be equal length: old_scores={len(old_scores)}, "
            f"new_scores={len(new_scores)}, old_pass={len(old_pass)}, new_pass={len(new_pass)}"
        )
        raise ValueError(msg)

    if n < config.min_pairs:
        return SkillReplayVerdict(
            verdict=VERDICT_INCONCLUSIVE,
            n_complete=n,
            n_regressions=0,
            n_improvements=0,
            note=(
                f"only {n} complete pair(s) (< min_pairs={config.min_pairs}) — "
                "not enough signal to judge the edit"
            ),
        )

    score_wr = compute_score_winrate(old_scores, new_scores, epsilon=config.epsilon)
    pass_wr = compute_winrate(old_pass, new_pass)
    n_reg = score_wr["n_control_wins"]  # OLD scored > NEW by > epsilon
    n_imp = score_wr["n_treatment_wins"]  # NEW scored > OLD by > epsilon
    pass_favours_old = pass_wr["recommendation"] == "control_wins"

    if n_reg >= 1 or pass_favours_old:
        verdict = VERDICT_REGRESSION
        parts = []
        if n_reg >= 1:
            parts.append(f"{n_reg} task(s) regressed (OLD scored >epsilon higher)")
        if pass_favours_old:
            parts.append("binary pass-rate favours OLD")
        note = "; ".join(parts)
    elif n_imp >= 1:
        verdict = VERDICT_NET_POSITIVE
        note = f"zero regressions, {n_imp} task(s) improved (mean_delta={score_wr['mean_delta']})"
    else:
        verdict = VERDICT_INCONCLUSIVE
        note = "no per-task differences beyond epsilon (all ties)"

    return SkillReplayVerdict(
        verdict=verdict,
        n_complete=n,
        n_regressions=n_reg,
        n_improvements=n_imp,
        score_winrate=score_wr,
        pass_winrate=pass_wr,
        note=note,
    )
