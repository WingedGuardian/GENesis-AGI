---
name: validating-merges
description: >
  This skill should be used when a session's job is POST-MERGE VERIFICATION —
  taking merged PRs and establishing whether the change actually does what it
  claimed, then recording the result against its `pr_verifications` obligation.
  "validate the merged PRs", "work the verification backlog", "did that PR
  actually work", "run the post-merge E2E". It owns the obligation ledger the
  repo-pulse worker fills. Do NOT load it for building a change and opening its
  PR (that is `genesis-development`), or for driving open PRs to merge (that is
  `closing-session`).
# MEASURED against the real scorer (`scripts/hooks/skill_injection_hook.py`),
# both directions, 6 advertised triggers and 10 control prompts, 2026-09-26:
# this name + keyword pair fires 2/6 advertised and 0/10 controls. Every other
# candidate scored the same 2/6 with MORE false fires. The numbers are here
# rather than in prose because the first draft of this frontmatter was measured
# WORSE THAN USELESS — 0/6 advertised, 3/10 controls — and nothing but running
# the scorer would have shown it.
#
# WHY ONLY 2/6, and why that is the ceiling rather than a gap to close. The
# scorer reduces a prompt to bare WORDS, reads THIS list only (never the
# description prose), does NO stemming, and has no phrase or co-occurrence
# matching. So "validate the merged PRs" yields {validate, merged, prs} and
# matches neither `verification` nor the name token `validating` — `validate` is
# a different word. The four misses are only reachable with words that are
# false-positive generators: `merged` fires on "get the open PRs merged", which
# is explicitly `closing-session`'s job, and `validation` fires on "the input
# validation is wrong here". Same wall `closing-session` documents; the
# mechanism gap is issue #1799. Load this skill BY NAME for those phrasings.
#
# DELIBERATELY ABSENT, each a measured or reasoned lone false-firer: `validator`,
# `validation`, `verify`, `merge`, `merged`, `e2e`, `backlog`. And `postmerge`
# was DROPPED because it can never match: the tokenizer replaces every non-alnum
# char with a space, so "post-merge" yields {post, merge} and the concatenated
# form is unreachable. `deliverable-builder` carries two dead keywords of exactly
# that shape (`take-home`, `one-pager`).
#
# The NAME contributes tokens at +2 each, which is why this is not
# `validator-session`: `session` fires on "restart the session" — measured, 3/10
# controls — and no keyword edit can reach a name token.
keywords: [verification, verifications]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

## Load Gate

Three session types touch a PR and they are not phases of one life:

| type | owns | skill |
|---|---|---|
| **build** | an item from Ready until its PR exists | `genesis-development` |
| **closing** | the open-PR queue, until merged | `closing-session` |
| **validator** | the MERGED PR, until its obligation is discharged | this one |

If the job is "make the change", you are a build session. If it is "get the PRs
merged", you are a closing session. If it is "did the merged thing actually
work", you are here.

---

## What you own

`pr_verifications` (issue #1718 half B). The repo-pulse worker opens one row per
merged PR; a documentation-only diff is auto-closed by a deterministic path rule;
**everything else stays OPEN until you record evidence.** Read the backlog with

```bash
python3 scripts/repo_pulse_worker.py --verification-backlog
```

and record what you concluded with

```bash
python3 scripts/pr_verification.py print-schema > ~/.genesis/output/prv-<N>.json
#   ... fill it in ...
python3 scripts/pr_verification.py close --pr <N> --verdict <one of the four> \
    --evidence-file ~/.genesis/output/prv-<N>.json [--note "..."] [--dry-run]
```

Read back what was decided, and on what evidence, with

```bash
python3 scripts/repo_pulse_worker.py --verification-log [--pr <N>]
```

Two mechanics worth knowing before your first run. **`--dry-run` resolves the
target and renders the record without writing** — use it, because a closed row
cannot be amended through this tool. And **`genesis_db_path()` is repo-root
relative**, so running the closer from a linked worktree points it at that
worktree's non-existent `data/genesis.db`: run it from the main checkout or pass
`--db-path`. The message says so, but the diagnosis is not the one you would
guess.

The backlog is PER INSTALL — each box's worker fills its own ledger against its
own deploy state, so two installs hold different open sets and neither one's
count says anything about the other.

---

## The four verdicts

Never pass/fail. Every PR gets exactly one of these (standing ruling):

| `--verdict` | meaning | the row | the route |
|---|---|---|---|
| `pass-mechanical` | the claim holds, measured | **CLOSED** | nothing owed |
| `pass-with-measured-gaps` | holds, with gaps named in `scope_limits` | **CLOSED** | file the gaps if reachable |
| `fail-intent` | the merged change does not do what it claimed | **stays OPEN** | to the USER, as a conversation |
| `cannot-verify` | the attempt could not reach a verdict here | **stays OPEN** | see the filing rule below |

**A FAIL-intent never triggers an automatic rollback** (standing ruling). It goes
to the user as a conversation. The row stays open because the obligation is not
discharged until the fix lands — and you are not the one who fixes it.

The two non-closing verdicts **require `--note`**, and the note is the whole point
of the row staying open: it is what stops the next validator re-deriving why this
could not be finished. It surfaces inline in the backlog as
`ATTEMPTED 2x cannot-verify — <note>`, so a parked row announces itself. Parked
rows also sort LAST in the backlog, behind never-attempted work — they are
typically the oldest, and an oldest-first reader would otherwise let them starve
the work you can actually do.

Three refusals the tool enforces, so you do not have to remember them: a closing
verdict is refused when any claim in the document has `verdict: fail` (that
document's verdict is `fail-intent`); `pass-mechanical` is refused when any claim
is tier `NOT_VERIFIABLE_HERE` (use `pass-with-measured-gaps`, or `cannot-verify`);
and a closing verdict refuses `--note` rather than silently discarding it.

---

## FILE AN ISSUE, or RECORD A CANNOT-VERIFY?

These feel like one question and are two: *what did the verification find* (the
verdict) versus *is there work anyone could pick up* (the route). Only the second
decides filing.

**The discriminator is a question, not a label — name the ACTOR and the
RESOURCE.**

- **Can you name a real actor and a real resource?** "a box without the graph
  client installed", "another install", "after the next deploy runs", "a clean
  venv". → **Reachable. This is ordinary work: FILE it.** A repo-owned defect is
  a GitHub issue; state local to one box is a `follow_up_create` row. Scrub
  anything install-identifying first — an issue carries technical detail only.
- **Does naming the actor require inventing a hypothetical?** Hardware nobody
  has and nobody has reason to get; an event that may never occur. → **Genuinely
  unverifiable. Do NOT file.** An unactionable issue burns whoever picks it up,
  and the repo already routes "cannot be picked up" to `tabled` rather than to
  issues. Record it **on the row** — `--verdict cannot-verify --note "<the
  precondition>"` — which leaves the obligation open and annotated, so the next
  validator on any install sees `ATTEMPTED … cannot-verify — <your note>` in the
  backlog and re-checks it once the precondition is reachable, instead of
  re-deriving your conclusion from scratch.

**The abuse this marking invites, and the test that stops it.** *"I did not get
to it" is never CANNOT-VERIFY.* Ask: **could I have done this with the access and
the time I actually had?** If yes, it is NOT-YET-DONE — leave the row open and
say so. Measured instance, the first validator session's own first PR: a
sub-claim was labelled unverifiable when it was reachable on that very box with a
throwaway venv; it had been skipped for cost. If CANNOT-VERIFY becomes the
routine escape, `closed` degrades to "we looked at it" and the word stops
meaning anything. Keep it RARE.

A security defect is the one thing never filed publicly before it is fixed —
private record plus a local row, whoever owns it.

---

## Before you judge anything: verify on the right code

**Merged is not deployed.** Confirm the box you are testing has actually pulled
and deployed the merge — from the running version, the installed artifact, or
the logs — before forming any verdict. A validator running against a tree that
predates the merge will measure a defect the branch already fixed and report it
with complete confidence.

**And do NOT establish deployment by ancestry.** MEASURED: `gh pr view --json
mergeCommit` on a STACKED PR names a commit reachable only from that PR's own
base branch, because `baseRefName` was a feature branch rather than the default;
the change reached the default branch inside the squash of its parent, under a
different PR number. `git merge-base --is-ancestor <mergeCommit> HEAD` therefore
returns a false *"not deployed"* while the code is live and behaviourally
correct. Establish it by **content** (the change is present in the deployed
artifact) or by **BEHAVIOUR** (the live thing was observed doing it), and check
`baseRefName` before trusting `mergeCommit` at all. Ancestry is a fast positive
path and never a negative verdict.

Report which copy of the code you actually exercised. A verdict that does not
say is not a verdict.

---

## The `E2E:` line is a LEAD, not a limit

A PR body may declare its post-merge verification. That declaration is the
author's hint from when they knew most — read it first. It is not the boundary
of the job: **assume an E2E is owed and hunt for the real effect.** A bare
`E2E: none` on a code PR is a reason to look harder, not a release.

Two things follow, both measured on the first validator session's pilots:

- **Roughly half the backlog carries no declaration at all**, so deriving the
  verification from the diff is the common mode, not the exception.
- **The undeclared surfaces are where the findings are.** A PR that fixes a
  RENDERER does not repair an already-rendered artifact; a PR that widens an
  uninstall path cannot be verified without running it. Neither was declared.

---

## Triage by risk, and sample — do not chase

The backlog outruns any per-PR cadence. Order by consequence: **gates and
enforcement hooks, then runtime and migrations, then everything else.** Deploy
main on a cadence and validate against that, rather than deploying per PR.

**Age is an ASSET, not a cost** — this inverts the obvious intuition and it is
measured. An old row whose behaviour is EVENT-GATED has had weeks for production
to run the experiment: one three-week-old PR yielded 19 real production events,
giving n=19 for both of its claims from a single query — the strongest evidence
of three pilots and the cheapest to obtain. A one-day-old scheduled job gave
n=37 runs. A hook PR had no production samples at all and needed synthetic
probes. So oldest-merge-first (which the backlog reader already does) is right
for a second reason: old rows are not a wall, they are the tier where the
experiment already ran.

---

## Negative controls are not optional here

MEASURED: negative controls caught two would-be FALSE FINDINGS in three pilot
PRs — a recurring journal warning that turned out to be environmental (every
sibling unit with the same hardening directive emitted it; the one without them
emitted none), and an apparently-dead error branch that was simply reached by a
different syntax than the probe used.

For every claim you verify, run the arm that must come out the OTHER way. A
guard that blocks the case under test would also block everything; only the
control tells those apart. A verification with no control is a story.

---

## Hard limits

- **Never merge.** That is the closing session's job and the user's approval.
- **Never write to the stores UNDER TEST**, and never run a migration against a
  live database. Read live state freely; do not mutate it to force a code path.
  The one store you DO write is your own obligation ledger — that is what
  discharging a row means, and it is not an exception to this rule but the
  subject of a different one.
- **Do not collide with a deploy.** Never restart services or run an E2E while
  another session might be deploying. Where the repo provides a deploy lock
  wrapper, run under it; where it does not yet, check for in-flight deploys
  before touching a service.
- **Separate INSTALL findings from REPO findings.** A failure may be specific to
  one box's configuration rather than a defect in the repo. Say which before
  filing anything, and file a repo issue only for the repo half.

---

## Evidence: what the record has to carry

The evidence document is validated by `scripts/pr_verification.py`; its shape was
derived from pilot validations rather than designed, because each pilot broke a
field the obvious version would have had:

- **`deploy.method` + detail** — because ancestry lies (above). HOW deployment
  was established travels with the row.
- **`claims[]` with a per-claim TIER** (`MEASURED` / `INFERRED` / `READ` /
  `NOT_VERIFIABLE_HERE`) — because a declaration DECOMPOSES and the row is
  binary. One PR's two declared claims resolved into four sub-claims across three
  tiers; a single verdict would have been true of some of it and false of the
  rest. An INFERRED claim is one you composed from two measured facts — label it,
  never promote it.
- **`controls[]`** — a reader cannot distinguish a non-vacuous verification from
  a vacuous one without them.
- **`scope_limits[]`** — what this install could not reach, named.
- **`findings[]`, each with a DISPOSITION** — an undispositioned finding is a
  drop wearing a record, and the validator refuses one.

---

## Devin's install-scoped PRs

Devin's install and hybrid PRs carry `E2E:` lines written for Genesis to run.
The validator is their natural consumer — those are the rows where the
declaration is most likely to be both present and actionable.
