# Due diligence before building

The canonical checklist is the SKILL.md section "Due diligence before building",
items 1-5. This file does not restate those items. It holds the detail behind
them: why the rule exists, how its bar fits the bars around it, and what each
item means in practice. Read it when writing a plan that will be presented for
approval, or when plan reviews keep finding things the plan should already have
known.

## Why it exists

The checklist used to be supplied by hand, as a pasted prompt before building.
On one install it was pasted into 164 of 593 Claude Code sessions over four
months (2026-05-28 to 2026-10-03). That was counted only among user-typed
messages; a raw text search over the transcripts double-counts, because
injected memory snippets quote the prompt. The rules behind most of it already
existed: CLAUDE.md's Confidence Framework, the five premise questions, and
`.claude/docs/confidence-framework.md`. They were not applied unprompted. The
advisory plan reminder (`scripts/hooks/plan_confidence_reminder.py`, merged
2026-09-16) coincided with a fall in how often the prompt was needed. The fall
was not to zero.

Three failures from one session on 2026-10-03 show what the bar prevents:

- **An unverified claim reached the plan.** The plan said a parser "fails
  closed, never open" on a class of input, and nobody had tried to construct the
  failing case. The plan review constructed it in minutes: the parser took a
  source pointer from inside a rendered code block. A free read (rendering the
  case through GitHub's Markdown API) would have settled it before the plan was
  written.
- **The owner was asked what the session should have decided.** There were
  seven questions about parser edge cases. Each had been measured at 0
  occurrences in 1,902 real PR bodies and already carried a recommendation, and
  the owner deferred all seven to the session's recommendations.
- **A plan sentence asserted a test that does not exist.** It claimed a CI byte
  budget on a file. One grep of `tests/` refuted it, and that grep was a free
  read.

## How the bars fit together

A question is **material** when the plan would change if its answer came out the
other way. That is also the test for whether a read is owed. The three existing
standards then apply in order:

- **Beyond reasonable doubt** (this rule) is the bar on every material
  question, before building.
- **Effort proportional to stakes** (CLAUDE.md, Advisory Output Standards) sets
  how DEEP to go on each material question. A typo fix has few material
  questions and shallow answers suffice.
- **Investigate anything below 90%** (CLAUDE.md, Confidence Framework) stays the
  floor: the point where investigation becomes mandatory regardless of stakes.

Reading everything is not the goal. Leaving a material question unread when a
free read would settle it is the failure.

## Free reads (item 1)

A free read settles a question read-only, or by a probe that changes nothing
beyond scratch state, using tools the session already holds. These are not free
reads, however reversible they look: a setting changed and then restored, a
service restart, a comment posted and then deleted (it was public while it
existed), or a write to a live store. The usual free reads:

- the code the change touches, read in full, and every caller (Serena
  `find_referencing_symbols` is live);
- the subsystem's entry in `docs/architecture/CURRENT.md`;
- recent commits and PRs in the area: `git log -- <path>`, and
  `gh pr list --state all --search "<term>" --limit 200`. Treat a full page of
  results as truncated, per CLAUDE.md's "Evidence Must Match";
- open PRs and issues that overlap, and peer sessions working near it (the
  per-turn concurrency lines; `ListAgents`);
- code intelligence for blast radius. CLAUDE.md says no code-intel tool is a
  mandatory pre-edit gate, and that holds; it is owed here only when the blast
  radius could change the plan;
- a probe of the external behaviour the plan relies on, such as a CLI flag, an
  API, or a renderer (`gh api markdown -f mode=gfm -f text=...` renders exactly
  what GitHub shows);
- a measurement over real data (merged PR bodies, live rows, transcripts),
  reported as `k/N` with the method, per the Acceptance Bar section.

**The banned sentence, scoped.** "I did not verify X, but it should be fine" is
banned where X is material AND a free read would settle it. That narrows
CLAUDE.md's evidence tiers rather than contradicting them: "unverified, but" is
still the right hedge for a claim that genuinely cannot be settled this session.
In that case name it as a residual, say why it cannot be settled, and say what
would settle it.

## Red-team (item 2)

- **Root cause:** what observation would show the diagnosed cause is not the
  cause? Look for it.
- **Consequences:** what does the change break? Consider callers, other
  installs, background sessions, and the next reviewer's predictable finding.
- **Alternatives:** name the options weighed, simplest first, and say why the
  chosen one beats the simpler ones. A plan with no alternatives has not been
  red-teamed.

The plan review (`genesis-architect`) does this again from fresh context. Its
findings fall under CLAUDE.md's "Verify agent output": re-derive them, then
fold the confirmed ones into the plan before presenting it.

## The plan body (items 3 and 4)

Item 3 applies to plans presented for approval in a foreground session.
Task-executor plans (`/task`, `TASK_INTAKE.md`) keep their own section contract.
A plan that outlives one session also carries the header in
`references/plan-docs.md`. The test plan in item 3 never includes the full
suite run locally; CI runs it (see "Pre-Commit Gate").

## Ask vs decide (item 5)

Asking about intent, priorities and trade-offs early aligns the plan. A
technical edge case that has been measured and carries a recommendation is the
session's to decide: state the call in the plan, where the owner can override
it at approval.

Item 5's carve-outs stay the owner's however small they look:

- the Design Principles in CLAUDE.md, plus any standing axioms an install adds;
- approval and sovereignty gates, and anything that would loosen one;
- irreversible or outward-facing actions;
- anything user-visible.

## What this does not do

Nothing checks a plan against this list. It is written by hand, and a plan that
skips it fails silently until a review or the owner catches it. Pointing the
advisory plan reminder's text here is a separate change.

Whether the rule changes behaviour is measured by re-counting the sessions in
which the checklist prompt still had to be pasted: user-typed messages only,
excluding tool results, `isMeta` entries, injected `<system-reminder>` text and
compaction continuations. Compare against the already-falling trend, not against
a fixed number.
