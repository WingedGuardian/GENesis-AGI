# spec-converge: what remains against the spec

A builder's own pass before the PR, for work built from a spec. It reads the
spec as the statement of what was asked, reads the code as it is now, and
lists what is still missing, partly done, contradicted, or built without being
asked for. When nothing remains, it changes nothing and says so. That is how
it stops.

Where it runs: `build-session`, "Verify BEFORE the PR", for any build that has
a spec. It is the builder's pass. At review time, the architect's Step 0.5
compares the diff with the plan; that step stays informational and does not
replace this one, and this one does not replace it.

## Inputs

- **Intent:** the spec, its plan and its tickets. These are the only statement
  of intent. A commit message, a PR body, a checklist or the builder's memory
  of a conversation is not. If the owner changed the intent after the spec was
  written, the change is real only once it is recorded in one of these
  documents, or on the tracker issue as the owner's own decision.
- **Standing rules:** as in `spec-analyze.md`, Inputs.
- **The code as it is now:** the branch's working tree. For missing, partial
  and contradicts, judge what the code does, not what changed; only the
  unrequested class looks at the branch's own changes (Step 2).

## Step 1: the intent inventory

List every requirement and acceptance item with an id. Use the `R-n` and `A-n`
ids from the spec's `spec-analyze` report when one exists; otherwise number
them by the rules in `spec-analyze.md`, Step 1. Add each plan decision that
obliges the build to something, and each ticket. Record the line each comes
from.

**A checked box is not evidence.** A ticket marked done, a task list ticked
off, a ledger row closed or a plan bullet that says "done" is a claim, and
this pass checks it like any other item. Point at the code, the test or the
observed run that meets the item, or the item is not met.

Bound the search to what the documents name: the files and components the plan
and tickets list, plus what a search for each requirement's subject finds. Do
not widen the scope beyond what the documents define.

## Step 2: classify each gap

For each item, look at the current code and record a gap only where there is
one. Every gap gets exactly one class:

- **missing:** the work is not there at all.
- **partial:** it is there and does not yet meet the item.
- **contradicts:** the code does something the spec, the plan or a standing
  rule says it must not. A standing-rule contradiction is CRITICAL.
- **unrequested:** the branch adds something no document asked for. Judge
  this class over the files the branch changes
  (`git diff --name-only $(git merge-base origin/<base> HEAD)`, where `<base>`
  is the PR's base branch) and only the lines it changes there; the code that
  was already there is not this build's to justify. What the repo's own rules
  require of every change is never unrequested: tests, a changelog fragment, a
  capability registration, docs the change makes stale. This pass never
  deletes anything. It lists the item, so the builder removes it or the PR
  states why it stays.

Each gap records the item it traces to (`R-4`, `A-2`, `plan:L88`, `T-3`), its
class, a severity, and the evidence: the file and line, or the observation,
that shows the gap. Severity:

- **CRITICAL:** a contradiction of a standing rule (not a departure from a
  guideline, which is HIGH; see `spec-analyze.md`, Inputs), or a core
  requirement missing entirely.
- **HIGH:** missing or partial on a core requirement or acceptance item.
- **MEDIUM:** partial on a secondary requirement, or unrequested work with no
  stated reason.
- **LOW:** polish, or unrequested work that is small and harmless.

## Step 3: write, or stop

**When there are new gaps**, the pass's only write is to add them, as new
items, in a new section of the build's plan file (`~/.claude/plans/<name>.md`;
where it goes is below):

```
## Convergence <UTC date, time>
- [ ] <imperative description> per <spec>:L<n> (<R-id>) (<class>, <severity>)
```

One item per gap, CRITICAL and HIGH first, each traced to the line of the spec
(or plan, or ticket) it comes from. It never edits the spec, the plan's
earlier sections, or an earlier convergence section, and it never renumbers,
reorders or deletes an item. It writes no code; doing the items is the build's
next step. A build with no plan file puts the same section in its session
output instead, since a spec is not the builder's to edit.

**A gap already listed in an earlier convergence section is never appended
again.** If it is still open, the report counts it as open. A gap leaves the
open count only by being closed in the code, or by being carried. Carrying is
marking that item's line `(carried: <reason>)`, and only this mark makes a
gap carried: an earlier listing alone does not. What may be carried:
- unrequested work the builder keeps, by the builder's own mark;
- a requested item (missing, partial, or a plan or ticket item) only when the
  owner has decided to drop or defer it, and the mark names where that
  decision is recorded (the issue comment, or the line in the plan). Dropping
  something the spec asked for is a change to the spec, never the builder's
  quiet choice;
- never a contradiction of a standing rule. That gap stays open until it is
  fixed.

**When no gap is open**, it writes nothing (no empty section) and still prints
the full report.

Run it again after doing the items. Put the report's `Outcome:` line in the PR
body under `## Testing`, so a reviewer can see the pass ran and what it left,
and list every carried gap there with its reason.

**Where the section goes.** Insert it above a `## ═══ SUPERSEDED BELOW ═══`
divider when the plan has one, since everything below that line is archive.
A build with no plan file keeps its open and carried items only in session
output, which a compaction or a new session loses; keep a plan file for any
build that may run converge more than once.

## The report

```
## Convergence: <spec file name>
Checked: <n> requirements, <n> acceptance items, <n> plan decisions, <n> tickets
| Item | Source | Class | Severity | Evidence | Remaining work |
|---|---|---|---|---|---|
Gaps: missing <n>, partial <n>, contradicts <n>, unrequested <n>; open <n>, carried <k>
Outcome: <one of the three below>
```

The `Outcome:` line is always the report's last line and takes exactly one of
three forms:
- `Outcome: converged`: no gap is open and nothing is carried, or only kept
  unrequested work is carried;
- `Outcome: converged-with-carried (<k> carried: <R/A ids of each requested
  item carried>)`: no gap is open, and at least one requested item is carried
  on the owner's recorded decision;
- `Outcome: open (<n> open, <m> newly added to <plan file or "session output">)`.

Adapted from `templates/commands/converge.md` in github/spec-kit, tag v1.4.0.
Copyright GitHub, Inc. MIT License; the full notice is in
`LICENSE-spec-kit.md` beside this file.
