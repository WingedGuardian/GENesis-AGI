# The premise check — is this the right change at all?

A code review asks whether the change is CORRECT. This asks whether it should
EXIST in this shape. The two fail differently, and only one of them is caught by
another review round.

Run it in two places: at **plan time**, when the shape is still free to change,
and at **pre-push**, before an external reviewer spends a round on it.

## At plan and issue time

The plan-time half runs before work starts, on the plan or the issue itself.
Every plan and every issue that specifies work answers the premise form: nine
numbered items, each starting with its label. Items 1 to 5 are the **Problem
block** (is this a real problem, and what would fixing it look like); 6 to 9 the
**Change block** (is this the right change).

```markdown
## Premise checks
1. Observed: <what happens, with the command, output and commit (a fresh main), and the local causes ruled out>
2. Intended: <what should happen>. Stated: <the doc, test, ruling or comment that says so | nowhere>
3. Cost: <who or what is affected, how often, how that was measured>
4. Searched: <existing issues, main and prior rulings: what was searched, what was found>
5. Acceptance: <what must hold afterwards>. Wrong fix: <the tempting fix that must fail | none, because ...>
6. Mechanism: <why this removes the cause; who calls it and what they do differently>
7. Alternatives: <for each new mechanism or newly computed fact: what already does or decides this, the simpler mechanism considered, and why not each>
8. Evidence: <every outside-behaviour claim and number, measured or cited>
9. Shape: <counted lines (`python3 scripts/pr_shape.py --base origin/main`), and the split or the Shape: reason>
```

The answers belong in the plan or issue body, not in a reviewer's first round.
`n/a — <reason>` is an answer; a bare `n/a` is not, and neither is a label with
nothing after it. Answer 2 must carry `Stated:` and answer 5 `Wrong fix:`, each
answered. `Stated: nowhere` is an honest answer: the change is then a proposal,
not a fix. Item 9's size rule and its bands are step 6 of "The method" below.

**Proposals.** An issue whose answer 2 says `Stated: nowhere` (quotes or bold
allowed; only that word counts) gets the `proposal` label from the premise gate
Action, and loses it when an edit gives answer 2 a citation. Clearing a proposal
is that edit, not a second label: the owner's decision replaces `nowhere` as the
citation (`Stated: owner ruling 2026-11-20, this issue`), and the issue is then
an ordinary one. The label mirrors the body and nothing reads it back: removing
it by hand clears nothing, and it stays as set until the gate next runs on the
issue (an edit, a reopen, a `bug` relabel, or `devin-ready` applied).
`devin-ready` on a proposal is removed with the `(aside)` comment, even when all
nine answers are there. Another wording that admits no citation, such as
`none — new behaviour`, is not read as one (owner ruling 2026-11-15): only the
word the form names.

**Who owes what.** An issue owes the Problem block, and the Change block too if
any of it is written. A plan, and an issue handed to Devin (`devin-ready`), owe
all nine. A plan or a PR body whose issue already answers the Problem block may
write `1. Problem: as #<N>` in place of items 1 to 5 (the issue is not fetched).
The six questions this form replaced map in: outside claims and numbers to 8,
existing code and scope limits to 7, the caller to 6, the shape to 9.

**The switch** (`FORM_EPOCH` in `scripts/hooks/plan_premise_gate.py`, 2026-11-22).
Plans and `devin-ready` owe the nine-item form now. An issue created on or after
the epoch owes it; one created before it keeps the old rule, under which a
complete six-question section still passes, so no older issue changes its label
state. `devin-ready` is different: it owes the nine on any issue, whatever its
date, so applying it to a complete six removes it again. A
six-question section that no longer completes something is answered with the
mapping above.

`## Premise questions` is accepted as the heading too. A plan that names a
repo-relative path (`src/`, `scripts/`, `tests/`, `.claude/`, `docs/`, `config/`,
`.github/`) outside code blocks counts as specifying work, and the ExitPlanMode hook
`scripts/hooks/plan_premise_gate.py` blocks it while the section is missing or an
answer is empty. That is all it checks: whether an answer is true stays the
reviewer's question.

**On an issue,** the same parser applies, through the premise gate Action
(`scripts/premise_gate.py`). An issue without its answers carries the
`premises-missing` label until it has them. Issues opened before the gate, and bug
reports filed through the bug-report form, are not newly labelled. The feature
form prefills the nine labels, so an answer starts with its label; the labels
alone are not answers.

**Before it is filed,** a foreground session's `gh issue create` is checked against
that same rule: the PreToolUse hook `scripts/hooks/issue_premise_gate.py` blocks a
create whose body would be labelled, and names the missing answers. It judges only
a body it can read in full (a `--body` literal with no `$` or backtick, a
`--body-file` that no earlier step can have written, or the heredoc in
`-F - <<'EOF'`). Earlier steps are trusted only from a closed list of programs
that cannot write a file (`cd`, `echo`, `grep`, `true`, `test`, `[`), run directly,
in a command with no `<(`, no `tee` and no `>` anywhere except the whole tokens
`2>&1` and `2>/dev/null`; a relative path after a
`cd` is never read.
bound for the configured public repo, and passes everything else.
Dispatched sessions are never blocked.

**A bug report filed before the switch answered different questions.** Its
question is whether the bug is real, not how to build a change, so an issue
labelled `bug` and created before `FORM_EPOCH` carries a `## Verification`
section instead, with five numbered answers:

```markdown
## Verification
1. <reproduced: how, and on which commit (a fresh main, not a stale checkout)>
2. <evidence: the exact command and its output>
3. <reporter-side causes ruled out: misuse, local config, a stale tree, credentials>
4. <searched: no existing issue, and no fix already on main>
5. <expected vs actual>
```

From the epoch on, the `bug` label no longer switches sections: a bug report owes
the Problem block like any other issue, whose items 1 (Observed), 2 (Intended)
and 4 (Searched) ask what these five asked. Reports filed through the bug-report
form stay exempt from the label; the form still asks the five. One residual,
stated rather than hidden: a form report later edited so that it no longer opens
with the form's headings loses that exemption, and then owes a Problem block the
form never asked for.

**`devin-ready` always needs all nine premise answers,** bug or not, because Devin
builds a change. Applying it to an issue without them removes it again, with an
`(aside)` comment naming the missing answers, because that label starts a paid
Devin session. Devin's own integration watches the label, so a session it has
already started is not stopped; the label is the visible control. The check runs
when the label is applied: editing the answers out later leaves `devin-ready` in
place. `premises-missing` always means the issue's OWN section is incomplete (the
Verification for a bug report filed before the epoch), so a verified bug report
that lacks the nine loses
`devin-ready` and gets the comment, but not that label.

**Check before applying the label.** The Action acts only after the label event,
and on a busy runner queue that can be minutes after Devin has started. So a
session runs `python3 scripts/premise_gate.py check <issue>` first, and applies
`devin-ready` only when it exits 0. Exit 1 lists the missing answers, or says
the issue is a proposal: fix the issue, or for a proposal get the owner's
decision and cite it in answer 2, never apply the label over them. Exit 2 means the issue could not be read,
which is not a pass. On a `bug` issue filed before the epoch it also lists missing Verification answers;
those do not change the exit code, because only the premise answers decide the
label.

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
worth making? If yes, it is context, not a premise. Where the plan or issue answers
the premise form, its answers are claims too: step 3 tests item 5, and step 5 tests item 7.

Each premise belongs to a **layer**: the **problem** (items 1 to 5: is this real,
and what would fixing it look like) or the **change** (items 6 to 9: is this the
right fix). Name it on the claim line (the output, below). Item 2's citation is
always one of the problem-layer claims: open the doc, test, ruling or comment it
names and read it, never take the citation's word for it. When it says `nowhere`,
the claim is that the change is a proposal: check that the PR does not present it
as a fix.

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

**Test item 5 as one of them.** Can the acceptance line fail? Name an outcome that
would fail it. Then run the named wrong fix against it: the acceptance must fail
that fix. Where the answer is `Wrong fix: none, because ...`, test that reason. An acceptance nothing could fail, or one the wrong fix passes, is a
finding, because it cannot tell the change apart from the fix the issue warned
against.

**4. Ask the EFFECT question, explicitly:** *what does the caller do differently
because of this output?* Verifying that a value ARRIVES is not verifying that
anything CHANGES. That gap is the single most common thing this check catches and a
correctness review does not — the code is right, the plumbing is connected, and the
consumer on the far end does nothing with it (or, worse, drops it at a second gate
nobody enumerated).

**5. Then the COMPARATIVE question**, which is the half a validity check leaves out:
given the premises that hold, is this the BEST available shape? **Start from the
author's item 7.** Confirm or refute each alternative it names, with evidence, as
for any premise. Search from scratch only when it says "none found" (or the form is
absent), and then say what you searched. In a review round that follows a round
with a `fix-now`, the previous round's reflection carries an `Alternative:` line
(owed from `review_reflection.ALTERNATIVE_CUTOVER_ISO`; an older one may lack it)
(what already does or decides the thing its fix builds, or `none found: searched
<what>`): judge that fix against it the same way, confirming or refuting it, and
treat a "none found" as a search to repeat, not a finding. Either way, look specifically
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
Design-premise: <SOUND | SOUND-BUT-INFERIOR | BROKEN>[ — <qualifier>]
Expected before checking: <one line>
P1 <TRUE | FALSE | UNPROVEN> (<problem | change>) — <claim> · <evidence> · <confidence>% · falsified by <x>
P2 <TRUE | FALSE | UNPROVEN> (<problem | change>) — …
Effect: <what the caller does differently — or "nothing", which is the finding>
Better shape: <none found | the alternative, named>
PR-shape: <OK (N counted lines, band, one concern) | SPLIT — PR1 <concern>, PR2 <concern> …, in dependency order>
```

**This block is read by code, so write it exactly.** A round reflection cites the
output as `Premise-evidence:`, and `scripts/review_reflection.py` reads the block to
check that the reflection restates what you concluded. The rules:

- The `Design-premise:` line starts at column 0, as plain text, with one verdict
  word. A qualifier follows a dash: `SOUND — UNPROVEN(2)`. Any other line at
  column 0 that starts with `Design-premise:` (in any case, with a space for the
  hyphen, or in bold) makes the whole output refused, and so does a heading or
  list item labelled `Design-premise:` that carries an uppercase `SOUND` or
  `BROKEN` (`## Design-premise: BROKEN`). So never write
  `Design-premise: SOUND (80%)`, or a bold or bulleted one, in a summary. A
  heading that only names the section, or asks a question in lowercase, is fine.
- Each claim line puts its verdict FIRST, right after its number, then a dash
  and the claim: `P1 TRUE — …`. A short qualifier may follow the verdict in
  parentheses (`P3 FALSE (partially) — …`); the verdict word is still the one read.
  A claim line with its verdict at the end is refused.
- That qualifier carries the claim's **layer** (step 1): `P1 TRUE (problem) — …`,
  `P2 FALSE (change) — …`. The parser reads ONE parenthesis: to add a qualifier,
  put it after the layer inside the same one, `P3 FALSE (change, partially) — …`.
  A second parenthesis refuses the whole block. When the verdict is not SOUND,
  its qualifier names the layer that failed:
  `Design-premise: SOUND-BUT-INFERIOR — change layer`. The effect-nil case
  (below) keeps its own wording: every claim holds there, so no layer failed. Both are
  free text the parser already accepts, and nothing reads the layer by code.
- The `Expected before checking:` line is required: one line, directly under the
  verdict line. If you restate your block in a summary, the Expected line may sit
  in either one, as long as that block also has claim lines. The claim lines follow it. One line per claim: do not wrap a claim
  line.
- The block is closed. From the verdict line to the next blank line, every line
  must be one of the block's lines: the verdict, the Expected line, the claims,
  then `Effect:`, `Better shape:`, `PR-shape:` and `Persisted-state:` (one line
  each), or a code-fence line. Anything else there refuses the whole output, naming its line, so leave a
  blank line before any other text, and put a blank line above the verdict line:
  the block begins there. Outside the block, only the `Design-premise:` rules in
  the first bullet apply; claims, Expected lines and other text there are not read.
- Plain text only: no bullets, bold or tables inside the block. Prose around it is
  fine, and so is a code fence around the whole block.
- One block per output. If you quote an example or an earlier check, indent its
  `Design-premise:` line or prefix every line with `>`, so it is not read as
  yours, and leave a blank line between your block and the quote. A summary may
  restate your verdict and any of your claims with the same verdicts; it may
  never add a claim or change a verdict. A re-check is written to a new file,
  never appended to the old one.

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
  (build-session, "Building a rework"). Say so in the spec.
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

The builder's side of the contract (build-session, "Building a rework")
is an acknowledgement on the old PR before building, then a rework section in
each new PR's body. The section reports against this spec: its split position,
each deviation with its reason, and each delegated question with its answer.
Unforeseen complications are expected, and the section is where they are stated.

## What the verdict is FOR — and what it is not

The output is a **judgment about direction**, handed to whoever owns the change. It
is not permission to rewrite it, not a substitute for the code review (run this
first, then review the code if the premises hold), and not a reason to close
anything — a reviewer session retires nothing. A premise that fails because
the code cannot land, while the problem it solves is still wanted, keeps the
PR open and routes it as below; only a dead problem closes a PR
(closing-session, "Superseded is about the problem").

**BROKEN routes through the ESTABLISHED disposition, not a new one — and the bar
is HIGH.** The repo already has a route for a change that is wrong at the premise
or structurally superseded, and it is NOT "hand it to a builder": it is a
foreground ARCHITECTURE conversation with the user, and where no user is present,
the `needs-architecture-session` label, the PR moved to draft, and a `ready`
follow-up naming the PR and the decision it awaits (a dispatched session's follow-up lands in the `tabled` lane; the intake gap is tracked in #2857). See the closing-session skill, "Some PRs are not a
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

**The evidence bar is TWO OR MORE independent signals.** The signals: findings
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
