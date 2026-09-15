# High-Stakes Verification

Read this when the work is a pre-release review, a bug hunt, a guard or gate
change, or a fix that must not regress. Not for ordinary work — applied
everywhere it burns cycles for nothing, which is the failure mode of a method
document.

Most rules here exist because their absence shipped a defect; those carry the
instance. What is already covered elsewhere is a pointer, not a restatement —
a second copy of an instruction is a second thing to keep right, and the copy
is the one that goes stale.

## Already covered — read there, do not re-derive here

| Rule | Read |
|---|---|
| Evidence tiers (MEASURED / READ / INFERRED / ASSUMED) | CLAUDE.md, "Evidence Must Match the Scope of the Claim" |
| A number without a denominator is not a measurement | same |
| A truncated listing is not absence | same |
| An "absent" claim needs enumeration, not a failed spot-check | same |
| **Vacuous tests** — the named shapes, and "would this still pass if the mechanism it names were deleted?" | SKILL.md, Test-First Discipline |
| **A RED that comes back GREEN has six causes**, the first being that the run never executed — abort when the command emits no result line | same |
| **Mutation housekeeping** — proving a mutation applied, restore placement, hashing, and PRESERVING a file that no longer matches what the mutation wrote | same |
| A corpus replay is structurally blind to a shape nobody has typed | same, and the guard-corollary block |
| Every harness needs a control expected to FAIL, wired as an abort | SKILL.md, guard corollaries ("pair it with a control that DOES flip") |
| Re-measure another agent's finding before acting on it | CLAUDE.md, "Verify agent output" |
| Acceptance bar + measured rate as the default method | SKILL.md, On-Load Mindset |

The rest of this file is what those do not cover.

## 1. One twin per clause

A compound condition is not one branch. Each guard shadows the ones behind it,
so a single test only ever exercises the first clause it trips. Write one test
per clause, and confirm each fails for its own reason.

## 2. Validate the matcher before trusting the rate

A rate produced by an unvalidated matcher is not a measurement, and it is worse
than no number because it looks like one.

Measured: a first-pass audit matcher reported **29** hits over a real corpus.
Every one was an artifact — it read one long flag as a recursive flag and
another as a force flag, so it matched ordinary commands that merely mentioned
the verb. The same corpus, with a matcher that had passed a 12-case acceptance
bar first, returned **0**. The difference between "29 findings" and "no
findings" was entirely the instrument.

Run the acceptance bar BEFORE the corpus, every time: a set of must-catch cases
and a set of must-not-fire cases. If the bar fails, the rate is not reportable.

## 3. Fix at the choke point, not the call site

If the defect can recur at a caller that does not exist yet, the fix is in the
wrong place. Put the guard where the ACTION happens so a future caller inherits
it.

Watch ORDERING especially. A backup, a prune or a validation placed AFTER the
operation it exists to make safe is inert, and reads as completely correct.

## 4. Generate, don't just read

Reading alone finds little. What finds things:

- plant mutants over the code you just changed and see whether anything notices
- fuzz the GRAMMAR of the input space, not your own bug history — a bug history
  contains only what has already bitten you
- run the thing on real input and diff PER ITEM, never on totals
- use an external oracle (a real parser, the actual runtime) rather than your own
  model of the syntax

A corpus replay and a generated matrix answer different questions and neither
substitutes for the other (SKILL.md carries the full form of this). When you
find one defect, search for its SHAPE across the codebase, not for the symptom.

## 5. Check what is already in flight before claiming novelty

Four findings in one session were measured correctly and were not new: two were
already fixed in an open PR with a larger denominator than the one I had, one
was documented-intentional behaviour named in a merged PR's own body, and one
had no measurable exposure. The measurements were right every time; the leap
from "I measured X" to "X is an undiscovered defect" was wrong every time.

Before writing a finding down as new, in this order:

1. open PRs touching the file (`gh pr list --state open --json number,files`)
2. the file's own git log, and the merged PR bodies it references
3. the code's own docstrings and comments — a documented deliberate behaviour is
   a design to argue with, never an oversight to patch

The cost of skipping this is not just wasted work; it is a confident wrong claim
in a durable record.

## 6. An agent's finding is a lead, and it errs in BOTH directions

Sub-agent output gets an independent pass before it drives anything. The
verify-agent-output rule in CLAUDE.md covers under-reporting. The direction it
does not emphasise is the other one, and both were measured in one session:

- One audit had two specific claims that were simply wrong when re-run, while
  its top-ranked severity item was never reproduced at all.
- Another **over-escalated**: it flagged a "security defect named in neither the
  diff nor any open PR" — the merged PR that shipped the behaviour named it
  explicitly in its own body, which that same agent had already read for a
  different finding.

Treat an over-escalation as seriously as a miss. Acting on one burns a session
and can put a false claim into a public record.

## 7. Suspect the checker — but only with cause

**Reach for this only when you have a specific reason to doubt a particular
tool.** The four that qualify:

- it disagrees with a second, independent measurement
- it reports a suspiciously round or absolute result
- it is brand new, or you have just changed it, AND its result is the one you
  wanted
- its output is identical across inputs that should differ

Applied as a general habit this is a cycle burner and a way to talk yourself out
of true results; this repo's tooling is usually right. That scoping is the rule,
not a caveat on it.

When one of those fires, ask: does the check cover the same population as the
claim? Is it answering an ADJACENT question? Did it narrow the data and report
success anyway?

The generalisable instance is narrower and worth internalising on its own: **a
query against the wrong FIELD returns a clean empty result indistinguishable
from a true negative.** Measured — a migration was declared unapplied by
querying the wrong column; the runner reads a different one, and the migration
was simply pending a restart. A failed grep is the same shape: a pattern that
does not account for line wrapping returns nothing and reads as absence.

## 8. Report honestly

Say what you measured, what you assumed, and what you did not check. Record your
own checker bugs — a mistake in the checking step is the most repeated defect
there is, and it is invisible unless written down.

A finding that shrinks under investigation is a SUCCESS of this method, not a
failure of it. The alternative was shipping it.
