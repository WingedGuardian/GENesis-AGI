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
| **Choosing a command/value by reasoning about an external tool** — do not; §9 below | here |
| **Instrument for the answer, not the alarm** — the trigger, and the one-question test for an instrument | CLAUDE.md, "Instrument For The Answer, Not The Alarm"; mechanics in §10 below |

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

## 9. A question about an external tool is a MEASUREMENT, never a choice

The trigger, stated mechanically so it cannot be reasoned around: **you are about
to ship a command, a value, a flag, or a procedure, and your reason for picking it
is a claim about how something outside this repo behaves** — git, the shell, the
harness, a provider API. At that moment the question "which of these is right?" is
not a judgement you are entitled to make. It is an experiment you have not run.

Run it. Then ship what the run says.

This is not the same rule as §4. That one is about FINDING defects in code you
wrote. This is about CHOOSING an answer whose correctness lives in another
program's semantics, where reading the manual feels like evidence and is not.

### Enumerate the space; never pick cases from it

Behaviour that depends on state has a STATE SPACE, and the cases you would think
of are the cases you already believe in — which is precisely why they pass. So:

1. List the AXES that independently change the behaviour. Not scenarios, axes.
2. Sweep the cross product. A few hundred cells is seconds of compute.
3. Score every candidate on every cell and read the table.

A candidate that wins on four hand-picked cases tells you nothing, because the
hand that picked them is the hand that got it wrong.

### Pre-register the predicate, the decision rule, and the losing outcome

Before looking at any result, write down: what counts as success per cell, how the
winner is chosen, and **what you will do if nothing passes everywhere**. That last
clause is the one that does the work — without it, the least-bad option gets
rationalised into "the right one", which is the same move that produced the
original guess with extra arithmetic attached.

### Control the instrument, or its numbers are decoration

Every sweep carries two controls, and they are not optional:

- an **ORACLE** arm that should be perfect. If it does not score 100%, the
  instrument or the predicate is broken and **every number in the run is void** —
  including the ones you like.
- a **NO-OP** arm that should fail. If it scores well, the scorer cannot see
  failure.

Treat a surprising sweep result as a suspicion about the harness first. Four
separate instrument bugs each arrived looking exactly like a finding: a scorer
that dropped a path from both sides of its own comparison, an oracle whose
teardown deleted the evidence it had just created, two success criteria that
contradicted each other so that nothing could satisfy both, and a decorated
directory name passed where a state label belonged — which made an arm report
that every candidate failed.

### Split the space where the question stops being well-posed

Sometimes the sweep reveals that one region cannot be scored at all — a comparison
measured against a moving reference, a capture of a state that no longer exists.
Report that region separately rather than folding it into the headline, and say in
the shipped artifact where its advice stops applying. A procedure that is right in
one regime and wrong in another is only safe if it names the boundary.

### The instance

A hook note told the reader how to undo a destructive git operation. The advice was
wrong FOUR times, each version chosen by reasoning about git's semantics and each
refuted by a reviewer constructing a state nobody had tried:

1. `stash apply --index` — believed to write conflict markers. Re-measured: it
   refuses and leaves the file untouched. The stated reason for abandoning it did
   not exist.
2. `checkout <snap> -- .` — loses a tracked DELETION, because path checkout is
   overlay-mode by default.
3. the two above as a chain, complete-form-first — `stash apply` exits 0 on the
   central shape while leaving the reverted file reverted, so the "complete" form
   completed nothing.
4. the same chain with the order defended — still 56/240.

The fifth version was not chosen. 320 enumerated states (intervening commit x
unstaged x staged x tracked deletion x untracked file x post-rewind activity x
same-file overlap x pathspec scope) x six candidate procedures x an oracle and a
no-op, predicate pre-registered. The answer was a three-step procedure scoring
240/240 where a single command managed at most 56 — and the sweep also found the
boundary no amount of reasoning had surfaced: that same procedure scores 0/80 once
the reader has committed, because its second step restores a HEAD that by then
contains the damage.

The measurement cost about twenty minutes. The four guesses cost three review
rounds, a merge-blocking finding each time, and a note that would have told someone
to run a command that quietly did not work.

## 10. Instrumenting an event you cannot re-trigger

§9 is about choosing a command by measuring. This is its sibling: choosing an
INSTRUMENT, when the thing you want to observe is intermittent and each
occurrence costs the user a recovery. The trigger and the one-question test
live in CLAUDE.md, "Instrument For The Answer, Not The Alarm"; enumeration
(§9, "Enumerate the space") and control arms (§9, "Control the instrument")
already bind here unchanged. What those do not cover:

**Observation and attribution are different capabilities, and the cheap one
looks sufficient.** A watcher that reports an event is easy; one that names the
actor usually needs a different mechanism, often a more privileged one. Decide
which you need BEFORE building, because discovering the gap costs one whole
occurrence — and on a rare event that may be the only one you get for hours.
Worked example: inotify reports that a file was deleted and structurally cannot
say by whom; fanotify carries the causing pid but only for a listener holding
`CAP_SYS_ADMIN` in the INITIAL user namespace, so inside an unprivileged
container the field is present and silently zero. Same event, same kernel,
different answer — and "permission denied" was a uid problem, not a capability
the environment lacked.

**A sampler cannot see an actor shorter than its interval.** Diffing process
tables every N seconds misses anything that starts and exits between two
samples, and the miss is invisible: the actor is in neither the appeared nor
the vanished set, so the log reads clean. If the suspect class is short-lived,
sample-based identification is the wrong instrument however fast you poll —
take the event-driven path, or accept that you are measuring timing only.

**State what each layer CANNOT see, in the artifact.** An instrument's blind
spot is part of its output. Writing it down is what stops the next reader
treating a clean log as evidence of absence — the same failure the vacuous-test
rules guard against, one level up.

**Beware instrumenting with a tool that is itself a participant.** A probe that
opens the resource under investigation joins the population you are measuring.
Prefer read-only surfaces (`/proc`, filesystem metadata) and say so in the
docstring, or you will spend a round distinguishing your own footprints from
the defect's.

## 11. Audit your CLAIMS, not only your code

A green suite is the PRECONDITION for this pass, never its result. The tests
check what the code does; nothing checks the sentences you wrote ABOUT what it
does — and those are what the next session builds on, because permanent record
has no tone of voice.

READ, and EXTERNAL to this repo — a sibling toolkit's methodology handed over
2026-09-22, not a Genesis measurement: its author reports the pass finding
defects a fully green suite had shipped past, each one a sentence that outran
its evidence.

**Claims live in ENUMERABLE places, which is what makes this mechanical rather
than a mood.** This is the canonical list; SKILL.md and CLAUDE.md point here
rather than carrying their own copies:

| Surface | Why it goes wrong |
|---|---|
| commit messages and the PR body | written last, from memory, after the measuring is over |
| docstrings touched by the diff | a claim about behaviour, aging independently of the behaviour |
| in-thread review replies | asserts a finding is fixed; nothing re-checks it |
| ledger evidence and follow-up resolution notes | the durable record of what happened |
| Genesis memories stored this session | permanent, system-wide, recalled as fact |
| changelog fragments | becomes a user-facing entry verbatim |
| issue bodies | a public record the next reader treats as measured |
| specs and evaluations | read later as a decision's basis, long after the basis moved |
| plan documents | line numbers, PR heads and counts go quietly false as the tree moves |

Then put each sentence on trial. Five steps, each carrying the instance that
bought it. The first four come from the single session that adopted this pass,
which is the argument for it; the fifth arrived later and is marked where it
sits:

1. **Re-compute every number written in prose, and name its denominator.**
   MEASURED 2026-09-22, counting `except` handlers whose body is only `pass`
   over the 1,291 **Python** files under `src/genesis/`: a two-line `grep` said
   **212**, an AST walk said **274** — a gap of **62 (22.6% of 274)** that the
   grep's single spelling never reached. Stated as the difference of totals,
   which is what was measured: whether the grep's hits were a strict subset was
   not checked, and a set-difference claim would need that.
2. **Re-measure anything taken before the tree moved.** Counting a different
   thing — handlers with no `raise` and no logging call, same tree — a peer
   session fast-forwarded the checkout mid-analysis and the count moved
   **1,154 to 1,155 across 409 to 410 files**. The stale figure was already
   written into a draft issue body.
3. **Verify the edit applied where you believe it did.** A `sed` correcting that
   number matched **2 of 3** sites; the third spanned a line break. Caught by
   reading the file back, not by the exit code.
4. **When a file and a tool disagree, RE-DERIVE — one of them is scoped to a
   different question.** An allowlist comment and a classifier docstring
   disagreed about whether skill-file findings score; calling the predicate
   settled it in seconds, and the comment-reading was the wrong one. The
   generalisation is already written up in SKILL.md's Pre-Merge Gate section
   under `_is_doc_path` — read it there.

Denominators for negatives are covered by the table at the top of this file and
are not restated here. The fifth step is not, and is stated rather than pointed
at — which is itself its instance:

5. **Re-read every evidence sentence against the artifact it names** — open the
   PR, the file, the row. An evidence string is a claim like any other, and it
   is the one written with the least attention, because by then the work feels
   finished. The instance is this section: its first draft declared this step
   "covered by the table at the top of this file", and it was in no row. A
   pointer to an obligation that does not exist reads exactly like the
   obligation being met, which is the failure this whole pass is about.

Two outcomes of this pass feel like failure and are not. A claim that SHRINKS
under audit is the method working (§8). And where one of §7's four causes fires,
the instrument is a legitimate suspect ahead of the code — in the session above
two of them did: a detector disagreed with an independent second measurement,
and a proposed fix was brand new and gave the answer its author wanted. Both
were wrong. Neither licenses suspecting your tools as a habit; §7's scoping is
the rule, not a caveat on it.
