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

## Two field values a reader cannot look up

JSON carries no comments, so the two least self-explanatory values are recorded
here. Both were verified against this repository's live ruleset rather than
inferred, because neither is documented by GitHub.

`bypass_actors[0].actor_id: 5` is the **repository-admin role**. GitHub publishes
no mapping from `RepositoryRole` ids to role names anywhere; 5 is confirmed by
reading the live ruleset back and seeing `current_user_can_bypass: "always"` for
the owner. This single number decides WHO can bypass the approval rule, so it
should never be changed on a guess.

`require_extra_approval_for_unattributed_changes` is absent from GitHub's
published OpenAPI schema yet accepted and echoed in production. It is declared
deliberately; a reader who cannot find it in the docs has not missed anything.

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
direct pushes to `main` are refused by the checks ruleset in the ordinary case
(below) plus the local push guard. Read the cost section for the case that
slips through, because it is narrower than "refused".

`pull_request` also carries `require_last_push_approval: true`. Without it an
approval survives the push that changes what it approved: a contributor's PR is
approved at one commit, the contributor pushes another, and GitHub still counts
the stale approval against the one-review requirement — so the code that merges
is not the code anyone reviewed (Codex P1, PR #1907). Requiring the LAST push to
be approved by someone other than its pusher closes that window without
recreating the self-approval deadlock, because self-authored PRs merge through
the admin bypass rather than through this rule.

## What is required, and why exactly these three

`test`, `leak-detector`, `lint` — all contexts of the `CI` workflow, matched by
the check-run name exactly as the rollup reports it.

They were chosen on one criterion: a red result must mean the change is
genuinely not mergeable, never that something unrelated is flaky.

**Two of the three read the whole tree, not the diff** — `leak-detector` scans
every tracked file, and `test` runs the full suite. An earlier draft of this
section claimed all three were diff-scoped and that only an Actions outage could
break one without breaking `test`. Both were false, and together they understated
the real availability risk: with NO bypass actors, anything that turns one of
these red blocks every merge in the repository, and the only escape is a human
disabling the ruleset in the settings UI.

The concrete version of that risk was an unpinned tool. A new `ruff` release
adding a default rule, or a new `detect-secrets` release adding a detector that
fires on pre-existing content, would have gone red on a diff that touched
nothing — and `test` would have stayed green, so nothing would have looked like
a cause. Both are version-pinned in `ci.yml` now, which is what makes the
selection criterion true rather than aspirational. `gitleaks` was already pinned
by version and checksum. Pin anything else these three jobs install.

Not required, and each for a reason: `review-depth-check` is advisory by design;
`CodeRabbit` is a third-party surface whose availability is not ours; `CodeQL`
is our own workflow but reports under matrix-named contexts and depends on
analysis-service latency, which is a different argument for the same conclusion;
the remaining `CI` jobs are worth keeping green but a stall in one should not
hold the repository.

## Squash only, in two places on purpose

`allowed_merge_methods` is a parameter of the `pull_request` rule, which lives
in the BYPASSED ruleset — so on its own it binds contributors and does nothing
for the maintainer, exactly as the destructive rules did before they moved. It
cannot be moved either: a second `pull_request` rule in the no-bypass set would
make the approval requirement bind the maintainer too, which is the deadlock
the split exists to avoid.

So the repository SETTING carries the half the ruleset cannot. Repo
merge-method settings are not bypassable, and `allow_rebase_merge` was `true`
until 2026-09-09 — the declared squash-only policy was not actually enforced for
anyone, including the maintainer. It is now `false`, alongside
`allow_merge_commit: false`.

Both, then: the ruleset is the reviewable record and the default a fresh
install inherits; the setting is what binds today. Neither alone was enough.

## The cost, accepted with open eyes

A required check that breaks — a workflow rename, a bad merge to `ci.yml`, a
runner outage — blocks EVERY merge until a human intervenes. There is no
session-level escape: that is the design, not an oversight. It also ends direct pushes to
`main` for everyone, the admin included: the `update` rule lives in the
approvals ruleset, but a required status check has nothing to attach to on a
bare push, so the push is refused. That matches the repo's own
never-push-to-main policy, and it is worth knowing before the first time
someone tries.

**With one hole, stated rather than papered over.** "Refused" holds for a bare
push of a commit no workflow has ever run on — there is no check run for that
SHA, so the requirement cannot be satisfied. It does NOT hold for a commit that
already carries green `test`, `leak-detector` and `lint` runs, which is exactly
what the head of an open PR is: the checks ruleset is satisfied by those
existing runs, and the rules that would otherwise demand the PR route —
`update` and `pull_request` — are both in the bypassed ruleset. So the admin can
fast-forward `main` onto an already-green commit, skipping approval and the
squash-only history, and no server-side rule stops it (Codex P2, PR #1907).

This residual is accepted rather than closed — a deliberate deferral, not an
impossibility. An earlier draft of this section claimed the only server-side fix
would recreate the self-approval deadlock. That was wrong, and it was the worst
kind of wrong: a confident sentence telling the next reader a closable hole
cannot be closed. Two remedies exist and are recorded here so nobody has to
rediscover them.

1. **`"bypass_mode": "pull_request"`** on this ruleset instead of `"always"`.
   The maintainer would still bypass the approval rule on a pull request, so no
   deadlock — but the bypass would stop applying to a bare push, leaving the
   `update` rule binding there. One word.
2. **A second `pull_request` rule with `required_approving_review_count: 0`** in
   the no-bypass ruleset. GitHub's rules allow zero approvals: the pull request
   must be OPENED, not approved. That demands the route without demanding a
   review.

Both are UNVERIFIED against GitHub's live behaviour, and the uncertainty is
specific rather than general. Rulesets targeting one ref AGGREGATE — there is no
priority between them, and where the same rule appears twice the most
restrictive version applies — so remedy 2 depends on bypass being evaluated per
ruleset BEFORE aggregation. If it is evaluated after, the one-approval rule wins
and the maintainer is locked out. Remedy 1 depends on `gh pr merge --admin`
counting as "on a pull request".

Neither is adopted here because both end emergency direct pushes for the admin
as well, and that is a sovereignty decision rather than a documentation fix. The
declared end-to-end check for applying this ruleset is where remedy 1 gets
tested, against the live repository, where the answer is cheap to obtain and
free to revert.

What stands in its place meanwhile is local, and weaker than an earlier draft
of this file claimed: the push guard is an APPROVAL GATE on pushes to `main`,
not a refusal, it runs only inside Claude Code sessions on a configured install,
and it does not exist for a plain shell or for a fork. It is nonetheless the
layer that has actually been enforcing this all along — worth knowing precisely
because the protection is not where a reader would assume it is.

Recovery is the
owner setting the checks ruleset to `disabled` in the repository's rules
settings (roughly a minute in the UI), landing the fix, and re-enabling it.
`scripts/apply_rulesets.py --dry-run` shows what would change before any write.

## Applying

    python3 scripts/apply_rulesets.py --dry-run     # diff only, no writes
    python3 scripts/apply_rulesets.py --apply       # reconcile

Idempotent: it matches an existing ruleset by NAME, updates it when the live
definition differs, creates it when absent, and leaves everything else alone.
It never deletes a ruleset it does not recognise.
