# Branch rulesets, as reviewable files

GitHub rulesets are repository settings, not repository content: they do not
travel with a clone, they are edited in a web form, and a change to one leaves
no diff, no review and no history. That is a poor home for the rules deciding
what may reach `main`. The JSON beside this file is the source of record;
`scripts/apply_rulesets.py` reconciles a repository to it.

## Why there are two rulesets and not one

They differ in ONE property — whether the repository admin can bypass them —
and that property cannot be set per-rule.

`approvals.json` **keeps the admin bypass, deliberately.** It carries the
pull-request rule, which requires an approving review. The sole maintainer
cannot approve their own pull request, so without a bypass every self-authored
change would be unmergeable. The bypass is what lets that rule exist at all for
the case it is actually for: a contribution from someone else, which a
maintainer *can* review. Removing it here would not tighten anything; it would
delete the rule's usefulness and force a different bypass elsewhere.

`checks.json` **has NO bypass actors, deliberately.** It carries the required
status checks. A status check needs no approval semantics — nobody has to
"approve" a green test — so it can bind every merge, including `--admin` ones,
without recreating the self-approval deadlock. This is the whole point of the
split: before it, a single ruleset with one bypass entry made *every* rule in it
advisory for the merging actor, so the required check was decoration.

## Which rules go in which, and the test that decides

One question settles it: **does the rule need approval semantics?** Only
`pull_request` does — it asks a human to approve, and a sole maintainer cannot
approve their own PR, so it needs the bypass to be usable at all. Nothing else
does. `deletion` and `non_fast_forward` are pure prohibitions: nobody
"approves" deleting `main` or force-pushing over its history, so leaving them
in the bypassed set made them decoration for the one actor most likely to
administer the branch — the same defect as a bypassed required check, one rule
over (Codex P1, PR #1907). They live in the checks ruleset, where they bind.

`update` and `creation` stay in the approvals ruleset. They are already
bypassed today, so leaving them is the status quo rather than a regression, and
direct pushes to `main` are refused by the checks ruleset anyway (below) plus
the local push guard.

## What is required, and why exactly these three

`test`, `leak-detector`, `lint` — all contexts of the `CI` workflow, matched by
the check-run name exactly as the rollup reports it.

They were chosen on one criterion: a red result must mean the change is
genuinely not mergeable, never that something unrelated is flaky. Each is
deterministic and reads only the diff. Lint is included even though it is the
least severe: it is fast and deterministic, and the only thing that breaks it
in a way it would not break `test` is an Actions outage, which blocks
everything regardless — so excluding it would buy no availability.

Not required, and each for a reason: `review-depth-check` is advisory by design;
`CodeQL` and `CodeRabbit` are third-party surfaces whose availability is not
ours; the remaining `CI` jobs are worth keeping green but a stall in one should
not hold the repository.

## The cost, accepted with open eyes

A required check that breaks — a workflow rename, a bad merge to `ci.yml`, a
runner outage — blocks EVERY merge until a human intervenes. There is no
session-level escape: that is the design, not an oversight. It also ends direct pushes to
`main` for everyone, the admin included: the `update` rule lives in the
approvals ruleset, but a required status check has nothing to attach to on a
bare push, so the push is refused. That matches the repo's own
never-push-to-main policy, and it is worth knowing before the first time
someone tries. Recovery is the
owner setting the checks ruleset to `disabled` in the repository's rules
settings (roughly a minute in the UI), landing the fix, and re-enabling it.
`scripts/apply_rulesets.py --dry-run` shows what would change before any write.

## Applying

    python3 scripts/apply_rulesets.py --dry-run     # diff only, no writes
    python3 scripts/apply_rulesets.py --apply       # reconcile

Idempotent: it matches an existing ruleset by NAME, updates it when the live
definition differs, creates it when absent, and leaves everything else alone.
It never deletes a ruleset it does not recognise.
