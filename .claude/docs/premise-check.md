# The premise check — is this the right change at all?

A code review asks whether the change is CORRECT. This asks whether it should
EXIST in this shape. The two fail differently, and only one of them is caught by
another review round.

Run it in two places: at **plan time**, when the shape is still free to change,
and at **pre-push**, before an external reviewer spends a round on it.

## At plan and issue time

The plan-time half runs before work starts, on the plan or the issue itself.
Every plan and every issue that specifies work answers six questions:

1. Is every claim about outside behaviour (GitHub, git, a provider API) measured or cited, not asserted?
2. Was the repo searched for existing code that already does this, with the result written down?
3. Do the scope limits block the obvious shared code?
4. Is the caller named and tracked (an issue), or is there a stated reason there is none?
5. Does every number say how it was measured (what was counted, by which script)?
6. Is the PR shape stated: the expected size in counted lines, and either a split into one-concern PRs, a `Shape:` reason when it will reach 500 to 1,000, or the owner's approval of the shape when it will exceed 1,000? (The size rule and its bands: step 6 of "The method" below.)

The answers belong in the plan or issue body, not in a reviewer's first round.

## Why it exists

Review rounds accumulate for two reasons that the round COUNT cannot tell apart.
A sound solution carrying defects converges — each fix closes a case and the next
round is shorter. A wrong-shaped one does not: each fix creates the surface for
the next finding, so the loop reads like whack-a-mole while it is a design error
accruing interest. An external reviewer will find defects in a wrong-shaped change
indefinitely and never say the shape is wrong, because nothing asks it to.

The author is the worst-placed participant to notice, holding both the sunk cost
and the read-model that produced the design. Hence a separate step, run by someone
else, that asks a different question.

## The method

**1. Extract the premises.** State the claims the change DEPENDS ON, as the author
would state them — from the PR body, the plan file, the commit messages. Usually
two to four. A premise is load-bearing: if it were false, would the change still be
worth making? If yes, it is context, not a premise.

**2. Record what you expect BEFORE checking.** One line. This is the control on
your own reading: a session that starts out believing the PR is doomed will find it
doomed, and a session invested in the PR will find it fine.

**3. Verdict each premise INDEPENDENTLY** — TRUE / FALSE / UNPROVEN — each with:
- the **evidence**: a measurement with its denominator, or a `file:line` read. Not
  an inference, and not another document's summary of the code.
- your **confidence**, as a number.
- the **falsifier**: what observation would overturn this verdict.

Do them one at a time. The pull is to check the first, find it sound, and let that
carry the rest; premises fail individually and the second one is where it usually
happens.

**4. Ask the EFFECT question, explicitly:** *what does the caller do differently
because of this output?* Verifying that a value ARRIVES is not verifying that
anything CHANGES. That gap is the single most common thing this check catches and a
correctness review does not — the code is right, the plumbing is connected, and the
consumer on the far end does nothing with it (or, worse, drops it at a second gate
nobody enumerated).

**5. Then the COMPARATIVE question**, which is the half a validity check leaves out:
given the premises that hold, is this the BEST available shape? Look specifically
for:
- an existing **chokepoint** the change re-implements or bypasses (the common one —
  a skipped helper is a call-site defect, not a missing feature, and the fix is to
  route through it, not to build a second path);
- a **simpler mechanism** that makes the same guarantee;
- a place the problem **disappears** rather than being handled.

A sound-but-inferior approach is a FINDING. Say so, name the better shape, and let
whoever owns the change decide.

**6. Then the PR-SHAPE question, every time:** should this arrive as ONE PR?
Size by COUNTED lines, not raw insertions: `python3 scripts/pr_shape.py --base origin/main`
(`count_diff` over a hardened diff) counts added and removed code lines and excludes tests,
prose, changelog fragments, blank and comment lines, counting a moved line
once. The rule it encodes (#2737):

| Counted lines | Expectation |
|---|---|
| under 500 | the target |
| 500 to 1,000 | a `Shape:` line in the PR body says why it cannot be smaller |
| over 1,000 | explicit owner approval |

The bands come from a study of 400 merged PRs (#2737), in which median review
rounds rose from 1 to 6 across six size bands. That study's counting method is
not recorded; #2775 tracks re-deriving the bands with this counter.

This is a GUIDELINE the building session weighs, not a wall (owner,
2026-10-05). A change can have a legitimate reason not to fit, and stating it
is enough. What is not acceptable is never considering size at all.

A change that is too large for no stated reason, or that carries more than one
concern, gets a SPLIT plan: the PRs in dependency order, one concern each,
roughly what each contains. Often the split is the WHOLE rework: the code can
be right and still need to arrive as several PRs, so `SOUND` with
`PR-shape: SPLIT` is an ordinary, complete verdict.

What the verdict does depends on where you run the check:
- **In a rework spec,** the split is part of the spec, decided before the
  builder starts.
- **On an ordinary open PR in review,** it is not a kick-back. Ask the author
  for the split or for a `Shape:` reason, and let them decide. (A Devin-built PR
  is the exception; its disposition is the decision table in closing-session,
  "Devin-built PRs".)

"Concern" here means one mechanism or behaviour change a reviewer can accept or
reject on its own. Prefer one per PR; like size, this is weighed, not
absolute. Being a rework is not itself a reason to stay unsplit.

## Two calibration controls

- **A check where every premise fails is a check to distrust.** Some premises
  surviving is what makes the failed one worth acting on. If you have refuted all
  of them, re-read your own evidence before writing it up.
- **Name the pull you are under.** A stalled, conflicting, findings-heavy PR creates
  real pressure toward "so it must be wrong", and a healthy PR's clean check creates
  pressure to write it up as a disappointment. Say in the writeup which way you were
  leaning.

## The output

```
Design-premise: [SOUND / SOUND-BUT-INFERIOR / BROKEN]
Expected before checking: <one line>
  P1 <claim> — TRUE/FALSE/UNPROVEN · <evidence> · <confidence>% · falsified by <x>
  P2 …
Effect: <what the caller does differently — or "nothing", which is the finding>
Better shape: <none found | the alternative, named>
PR-shape: <OK (N counted lines, band, one concern) | SPLIT — PR1 <concern>, PR2 <concern> …, in dependency order>
```

### Resolving the verdict — the cases that are otherwise undecidable

The three verdicts must be decidable from what you found, so these are stated
rather than left to judgement:

- **A load-bearing premise you could not settle** does not make the change
  broken — it makes your check incomplete, and those are different claims.
  Emit `SOUND — UNPROVEN(n)`, naming which premises and what would settle them.
  UNPROVEN never supports BROKEN: you cannot hand a change back on evidence you
  do not have.
- **`Effect: nothing` IS broken**, and this is the case worth being precise
  about, because an earlier draft of this file said the opposite two paragraphs
  apart. BROKEN means "cannot do what it says it was built to do" — and a change
  whose consumer does nothing differently is the purest instance of that, so it
  routes like any other BROKEN verdict. What it does NOT mean is that a premise
  was false: every stated premise can be true and the change still land on
  nothing. Say exactly that in the writeup — `Design-premise: BROKEN — premises
  hold, effect is nil` — because "your reasoning was right and the change still
  does nothing" is a different conversation from "your reasoning was wrong", and
  the builder needs to know which one they are having. Raise it on the severity
  ladder too (normally BLOCKER).
- **No stated premises anywhere** — no PR body, no plan file, uninformative
  commit messages — is common on a fresh branch. Reconstruct the premises from
  the diff and SAY that you did. A reconstructed premise can never carry a
  BROKEN verdict: you would be refuting your own reading of someone else's
  intent.
- **A finding needs a rung or nothing sees it.** `SOUND-BUT-INFERIOR` is prose;
  the review's severity ladder is what the evidence validator and the merge gate
  score. Render the better-shape finding on the ladder as well — normally
  SHOULD-FIX — or it exists only in a paragraph.

## Handing a verdict to a builder: the rework spec

When a check leads to a send-back, the verdict becomes a SPEC, posted as a
maintainer comment headed `## Rework spec` (on a Devin-built PR, whether it
carries `(aside)` follows the `(aside)` table in closing-session, "Devin-built
PRs": a spec Devin is to build never does), that another session,
often Codex or Devin on another machine, builds from cold. Everything that spec
leaves out, the builder decides alone, and a reviewer later cannot tell a
misreading from a real complication. So the spec is decision-complete:

- **Keep:** what carries over unchanged, named by function or file.
- **Delete:** the machinery that is going away, named.
- **Mechanism:** the prescribed shape of the change, including which existing chokepoint to
  route through.
- **Split:** the `PR-shape` plan, as the list of PRs.
- **Decided questions:** every question the builder will hit, answered. A
  question only the owner can answer goes to the owner BEFORE the spec is
  handed over. One you deliberately leave to the builder is marked "builder
  decides; answer it in the PR body with your reasoning". Never end a spec with
  a bare "Open questions" list. A builder answers those silently, and a
  fail-closed answer can force design growth the spec never asked for.
- **Acceptance:** what the reviewer will check first.
- **Contract:** the builder acknowledges the spec on the OLD PR before
  building, and each replacement PR carries a `## Rework` section
  (genesis-development, "Building a rework"). Say so in the spec.
- **Follow-up:** the spec's last line, `Follow-up: <id>`, naming the follow-up
  the closing session opened before posting it. The builder copies
  it into the replacement that merges into main last.

Keep the old PR's `needs-rework` or `needs-architecture-session` label on, so
the rebuild stays traceable to it.

Instance, 2026-10-05: a spec without a split plan, ending in two open
questions, came back as one PR: +2,538 raw lines across 21 files, 881 counted,
which is inside the `shape` band, so size alone was not its failure. It carried
several concerns, three of them features its spec never asked for. The builder had answered
the deciding question in the most fail-closed way, and three of round 1's six
findings landed in machinery that answer produced. The premise was right, and the
spec still allowed the failure.

The builder's side of the contract (genesis-development, "Building a rework")
is an acknowledgement on the old PR before building, then a rework section in
each new PR's body. The section reports against this spec: its split position,
each deviation with its reason, and each delegated question with its answer.
Unforeseen complications are expected, and the section is where they are stated.

## What the verdict is FOR — and what it is not

The output is a **judgment about direction**, handed to whoever owns the change. It
is not permission to rewrite it, not a substitute for the code review (run this
first, then review the code if the premises hold), and not a reason to close
anything — a reviewer session retires nothing.

**BROKEN routes through the ESTABLISHED disposition, not a new one — and the bar
is HIGH.** The repo already has a route for a change that is wrong at the premise
or structurally superseded, and it is NOT "hand it to a builder": it is a
foreground ARCHITECTURE conversation with the user, and where no user is present,
the `needs-architecture-session` label, the PR moved to draft, and a `ready`
follow-up naming the PR and the decision it awaits (a dispatched session's follow-up lands in the `tabled` lane; the intake gap is tracked in #2857). See the genesis-development skill, "Some PRs are not a
review problem". This check produces the EVIDENCE for that conversation; it does
not invent a parallel path around it, and a session that reads "hand back" as
"dispatch a builder and move on" has skipped the decision the label exists to
force.

Recommend it only when the premise is genuinely wrong, or the change cannot do
what it says it was built to do — a major rework, or a material finding that moves
the whole premise. Everything short of that stays in the gate and gets iterated
on: a premise slightly off, needing modest rework a review session can carry in a
round or two, is the ordinary case and is NOT a kick-back. (One owner-ruled
exception: a Devin-built PR is audited before further rounds are spent, and a
SOUND-BUT-INFERIOR verdict with a better shape that changes the mechanism or the
files touched IS kicked back to Devin. See the closing-session skill, "Devin-built PRs".)

**The evidence bar is TWO OR MORE independent signals**, the same bar the
round-2 gate message states, because a doc that set a lower one would be the
easier surface to read and would quietly undercut it. The signals: findings
CONCENTRATING in one file or function; a finding landing on a line THIS change
added in an earlier round; the diff GROWING across rounds instead of shrinking.
One alone is an ordinary local defect wearing an architectural shape — findings
concentrate in any large parser, and that on its own says nothing. **Short of
two, the answer is the class-level audit, not a hand-back.**

(That sentence exists because its absence was found by a reviewer, not by me: I
fixed the one-signal wording in the gate's message and left this document — the
one the message points at — still saying it. Second instance of a two-instance
class, which is the defect shape this whole PR is otherwise about.)

Two failure modes, and the second is worse than the first:

1. Kicking back too much. The gate stops working with the author, and the goal is to
   merge good changes, not to filter them.
2. Kicking back and being WRONG — the change was fine and the check was not. That
   costs more than any extra review round, because it discards correct work and
   teaches everyone to discount the verdict.

So when the evidence is thin, the answer is SOUND-BUT-INFERIOR with the better shape
named, or an ordinary finding — not BROKEN.

**And when it IS broken, say the other half out loud.** The code written so far is
what bought the understanding of why this shape does not work; that is what it was
for. Sunk cost is not a reason to keep patching, and a change handed back is not a
failure to be minimised — it is the check doing the job it exists for.
