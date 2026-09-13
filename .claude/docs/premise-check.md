# The premise check — is this the right change at all?

A code review asks whether the change is CORRECT. This asks whether it should
EXIST in this shape. The two fail differently, and only one of them is caught by
another review round.

Run it in two places: at **plan time**, when the shape is still free to change,
and at **pre-push**, before an external reviewer spends a round on it.

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

## What the verdict is FOR — and what it is not

The output is a **judgment about direction**, handed to whoever owns the change. It
is not permission to rewrite it, not a substitute for the code review (run this
first, then review the code if the premises hold), and not a reason to close
anything — a reviewer session retires nothing.

**BROKEN routes through the ESTABLISHED disposition, not a new one — and the bar
is HIGH.** The repo already has a route for a change that is wrong at the premise
or structurally superseded, and it is NOT "hand it to a builder": it is a
foreground ARCHITECTURE conversation with the user, and where no user is present,
the `needs-architecture-session` label plus a `ready` follow-up naming the PR and
the decision it awaits. See the genesis-development skill, "Some PRs are not a
review problem". This check produces the EVIDENCE for that conversation; it does
not invent a parallel path around it, and a session that reads "hand back" as
"dispatch a builder and move on" has skipped the decision the label exists to
force.

Recommend it only when the premise is genuinely wrong, or the change cannot do
what it says it was built to do — a major rework, or a material finding that moves
the whole premise. Everything short of that stays in the gate and gets iterated
on: a premise slightly off, needing modest rework a review session can carry in a
round or two, is the ordinary case and is NOT a kick-back.

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
