---
name: spec-session
description: >
  This skill should be used when a session SPECIFIES a Genesis change before it
  is planned or built: shaping the problem, the PRD, the architecture and the
  tickets a build session will take. It is the default for a foreground
  development session whose type the owner did not state. Load
  `genesis-development` (the shared core) first. Do NOT load it for building
  (`build-session`), closing open PRs (`closing-session`), or post-merge
  verification (`validating-merges`).
# MEASURED with the real scorer on this repo's skills, 2026-11-24: it fires on
# "draft a PRD for the backup change", "write the PRD", "let's start a spec
# session" and "new spec session for the voice bridge" (4/4), and on none of
# 10 controls ("see this spec and let's discuss", "write a spec for the backup
# change" and "the reviewer should read the spec" score 1.0 against a
# threshold of 2.0; "review PR 4917", "deploy main" and five more score 0). `spec` and `specs` were
# dropped as keywords so a prompt that only mentions a spec does not fire it; the
# session's type is the router's call (`genesis-development`).
keywords: [prd]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

# Spec session

**Load `genesis-development` (the shared core) first** if this session has not:
it holds the review-chain, evidence and private-data rules every session uses.

A spec session shapes one change into a spec: the document a build is checked
against. It writes no code in the repo and ends at the handoff; a new session
plans and builds from the spec. Where the owner keeps one session for both, it
compacts before building.

## Skipping the spec stage

A change on the skip list needs no spec stage and no owner-approved plan. Any
other change skips it only when the maintainer says so in conversation. A
session never decides that alone, and a board drag is not a skip.

The skip list:
- changes to docs or comments only;
- a fix confined to one file that `classify_change_substantiality` in
  `scripts/review_scope.py` classes `inline` (an `unknown` result does not
  qualify);
- revert commits;
- a change to install-local configuration only, outside the repo;
- a change of 10 or fewer counted lines, as
  `python3 scripts/pr_shape.py --base <base>` counts them; since that count
  reads commits only, a session using this entry checks it again after its
  commit and before any push, and a change over 10 lines stops there, with the
  commit unpushed, and goes to the owner.

Dependency bumps are not on it. A session using the list says which entry
applies and, except for the 10-line entry (checked after its commit, above),
checks the staged diff against that entry again before its commit; a change
that has outgrown the entry stops and goes to the owner.

## The process

1. **Understand the ask and what already exists.** Ask the owner at most 5
   questions before writing, each with a recommended answer, and record each
   answer under a `## Clarifications` section of the spec, dated, in the
   owner's words. (The question budget and the clarification log are adapted
   from spec-kit; the notice is `references/LICENSE-spec-kit.md`.) Before any
   claim that Genesis lacks something, read `references/existing-capabilities.md`.
2. **Search for what can be adopted.** Run the `evaluate` skill for any
   external candidate. When the spec would add repo-local code that decides
   something about a pull request, read `references/github-native.md` first.
3. **Write the spec**, outside the repo, at
   `~/.genesis/output/specs/YYYY-MM-DD-<topic>-design.md`. Read the core's "Due
   diligence before building" and `genesis-development/references/due-diligence.md`
   before writing its confidence and premise answers, and `build-session`'s
   "Generalizability Gate" before it names where code runs. Every spec carries
   these sections:
   - `Summary`: what, why and how;
   - `Decided`: what the build must hold to, including any mechanism the owner
     wants kept, since the spec is the document the builder does not own;
   - `Ruled out`: what the build must not do;
   - `Acceptance`: checks a reviewer can run before merge;
   - `Definition of Verified`: what is checked after merge;
   - `Confidence`: per item, each with what would disprove it;
   - `Premise checks`: the form in `.claude/docs/premise-check.md`.
4. **Run `references/spec-analyze.md`** over the spec, plus its plan and
   tickets when they exist. Every CRITICAL finding is fixed in the spec (then
   run the pass again) or carries the owner's exception. A conflict with a
   standing rule is never accepted by the session: when one looks justified,
   ask the owner through `AskUserQuestion` rather than rewording the spec
   around the check, and on a yes record it exactly as `spec-analyze.md`,
   "Owner exception", says. Fix every HIGH finding, or keep it with a one-line
   reason under the report. Save each run beside the spec as
   `<spec stem>.analysis.md`.
5. **Get one architect review** of the finished spec (the core's
   "Architecture Review"). Re-derive its findings before folding them in, then
   run step 4 again.
6. **Get the owner's approval**, then set the spec's frontmatter
   `status: approved`, with the date and the owner's words. A status counts by
   its first word.
7. **Save the final analysis report**: run `spec-analyze.md` once more on the
   approved text and save it once as `<spec stem>.approved.analysis.md`, never
   overwritten. It is the authority for the spec's ids and its sha256; after
   approval the spec changes only through its `## Amendments` section, the
   file's last section, headed exactly so. A CRITICAL or HIGH finding in this
   run goes to the owner through `AskUserQuestion`, and the answer is recorded
   as an amendment. When the handoff is an issue, save the report beside the
   spec all the same, and also post a verbatim copy as a comment on the issue, after the
   privacy scrub every public text gets (CLAUDE.md, "Public-Artifact Privacy").
8. **Write intent scenarios, when the install keeps a holdout.** If
   `.venv/bin/python scripts/holdout_runner.py --has-holdout` (from the main
   checkout) prints `holdout: present`, spawn the `holdout-writer` subagent IN
   THE FOREGROUND with the issue number and "intent". It adds scenarios that
   fail on the default branch until the change lands, and reports only their
   ids and `Protects` lines. Keep the scenarios out of the issue and the spec,
   and put this line in the issue: "The PR that finishes this issue must say
   `Closes #N`; if the work is split, earlier PRs say `Part of #N`." An intent
   scenario is judged only against a PR that closes its issue.
   `docs/reference/holdout.md` has the rules.
9. **Hand off.** A handoff file carries, within its first 10 lines, a line
   `For: <type> session`, where the type is one of `spec`, `build`, `closing`
   or `validating`. A spec carries no such line. The builder runs
   `references/spec-converge.md` against the spec before its PR
   (build-session, "Verify BEFORE the PR"), using the approved report's ids.

## Specs and plans are the record

Specs (`~/.genesis/output/specs`) and plans (`~/.claude/plans`) are the record
of approved departures, so both belong in the install's off-site backup:
list them in `GENESIS_BACKUP_EXTRA_DIRS`, which the "Opt-in extra directories"
step of `scripts/backup.sh` reads.

## Plan documents carry a structured header

A plan doc that outlives one session opens with YAML frontmatter naming what it
commits to and what it was written against (`status`, `pinned.main`,
`binds`/`prevents`, the trackers it executes). The template, its rules and its
rationale: `.claude/skills/genesis-development/references/plan-docs.md`.
