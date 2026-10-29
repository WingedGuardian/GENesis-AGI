# Incidents behind closing-session rules
Moved verbatim from SKILL.md, where each id is cited at the point it stood. The rules live
in SKILL.md; this file keeps the measurements behind them.

## opf-1 (old-pr-freshness)

Observed twice in one session on 2026-09-15 (unquantified: nobody
counted the old PRs that WERE still work, so treat this as a prompt to check,
not a base rate).

## opf-2 (old-pr-freshness)

MEASURED on a
two-commit branch whose work had landed as one squashed commit:

| test | result |
| --- | --- |
| `git merge-base --is-ancestor <branch> main` | rc 1 — reads as UNMERGED |
| `git cherry -v main <branch>` | `+` on BOTH commits — reads as unique |
| `git diff origin/main..HEAD -- <files>` | **empty — correctly detects it** |

## opf-3 (old-pr-freshness)

MEASURED 2026-09-15, one worktree, the cost of skipping the §0 head check (and
build-session, "Date the code before classifying a red"): a tree four commits
behind its PR head produced a CRITICAL finding, a full adversarial audit, a
public issue, a commit and review evidence — all describing a defect the branch
had already fixed in a commit that tree could not see. The same audit declared
two functions absent from the codebase when both exist at the head, which turned
a reviewer's correct finding into a rejected one. Every measurement was true of
the tree it ran in and false about the PR.
