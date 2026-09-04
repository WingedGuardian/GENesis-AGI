---
name: genesis-development
description: >
  This skill should be used when developing, debugging, refactoring, or
  building Genesis itself — tasks like "fix this in Genesis", "add a new
  MCP tool", "wire up the runtime", "Genesis won't start", "create a
  worktree", "debug the bridge", or "add a capability". Applies to any
  task modifying files under src/, .claude/, or tests/. Do NOT load for
  Genesis-as-tool work ("summarize this", "write a LinkedIn post",
  "research X") or general questions unrelated to Genesis internals.
consumer: cc_foreground
phase: 10
skill_type: workflow
---

## Load Gate

Before reading any reference, confirm the task is Genesis-*development*,
not Genesis-*as-tool*. If uncertain, ask the user: "Are we modifying
Genesis itself, or using Genesis for something else?"

## On-Load Mindset

Internalize these immediately when this skill fires — they shape how to
work from the start, not just what to check before commit.

### Wiring Discipline

Every new component needs at least one call site in the actual runtime
path. Apply this 4-level verification taxonomy:

1. **Exists** — file/function present. Proves nothing.
2. **Substantive** — tests pass, handles happy + error. No runtime proof.
3. **Wired** — live call site, import chain unbroken. Minimum for "done."
4. **Data-Flow Verified** — real data flows end-to-end. Required for
   critical paths.

Mark nothing "done" below Level 3.

### GROUNDWORK Code Is NOT Dead Code

Code tagged `# GROUNDWORK(feature-id): why` is intentional future
investment. Never delete or refactor it as dead code. Only remove when
the feature is fully active or the user explicitly cancels it.

### Architecture Review

For medium-to-large Genesis work (3+ files, new components, wiring
changes), dispatch a `genesis-architect` subagent before implementation
to check dependencies, edge cases, and DRY violations. Small targeted
changes skip this.

### Timeout Policy

The burden of proof is on you to justify why a timeout should exist.
Do not default to "add a timeout for safety." Instead:

1. **Identify the specific failure mode.** What hangs? Why? Is there
   evidence this actually happens, or is it speculative?
2. **Justify the specific value.** Why this number and not another?
   What legitimate work would be killed at a lower value?
3. **If you have no strong justification for a specific value, default
   to 2 hours (7200s).** This is the project floor — generous enough to
   never interfere with legitimate work while preventing permanent
   resource lockout from truly hung processes.
4. **Surface the request to the user** with the value, the failure mode,
   and the evidence. Never add a timeout as a "small improvement" or
   "defense in depth."

Timeouts on reflections, CC calls, cognitive paths, and long-thinking
work fight Genesis instead of helping it — they cap legitimate long
thinking and add speculative defense against rare hangs. The exception
is raw subprocess calls with no external watchdog (e.g., deterministic
executor steps), where a hung process blocks shared resources (executor
semaphore) with no other recovery mechanism.

**The Bash TOOL's own timeout is separate — default 120000ms, HARD CEILING
600000ms (10 min).** An inner `timeout N …` INSIDE the command does NOT extend it:
the tool wrapper SIGTERMs the whole call at its own `timeout` param (default
120000ms → exit 143). To allow longer, set that `timeout` PARAMETER explicitly —
**but you cannot exceed 600000ms.** A larger value does not buy more time; the
call still dies at 10 minutes (MEASURED 2026-08-27: `timeout: 1600000` was killed
at exactly `10m 0s`). Anything that might run past ten minutes therefore has only
ONE correct form — `run_in_background: true`. Treat "raise the timeout" as a fix
that tops out, not one that scales.

For long or unbounded work — above all deploys (`scripts/update.sh`,
`bootstrap.sh`, `host-setup.sh`: container align + guardian redeploy + host
`update-node`/`update-cc`, run sequentially — routinely exceed 600s and so CANNOT
be done in the foreground at any timeout value) — run via **`run_in_background: true`**
(harness-tracked, notifies on completion, no timeout ceiling), NEVER a foreground
timeout or `nohup … &` (detached but untracked → no completion signal, so you end
up hand-polling anyway). `update.sh` has SIGTERM/INT rollback traps, so a mid-run
kill is not a no-op — verify state (server active, no mid-rebase, pin, both CC
versions) before re-running; it is idempotent. (A non-blocking PreToolUse advisory
hook, `.claude/hooks/cc-deploy-timeout-guard`, nudges toward this when a deploy is
run in the foreground.)

### Verify Outcomes, Not Just Tests

`ruff check . && pytest -v` is the minimum bar, not the finish line.
After tests pass, verify the actual end-to-end outcome the change
delivers. Diff behavior between main and your changes when relevant.
For wiring changes: verify the init/bootstrap order passes the right
values at runtime, not just that parameters exist. For notification
changes: verify the notification actually arrives. Ask: "If the system
restarts right now, will this actually work?" If you can't answer yes
with evidence, you're not done.

**Verify in the REAL runtime context, not a shell proxy.** "Works when I
run it" is not "works where it runs." Same code + same uid ≠ same context:
a long-running systemd service (genesis-server, guardian) differs from your
interactive shell in mount namespace, seccomp, `NoNewPrivileges`, dropped
caps, and — the one that bites — **ptrace/`/proc` access**. Anything that
reads `/proc/<other-pid>/{environ,mem,stat}`, another process's env, sockets,
or namespaced/hardened resources MUST be verified by hitting the **live
endpoint** (`curl` the real server) or running inside the real service — not
`python -c` in your shell. (Origin, 2026-08: the CC-slot stale-code badge
read `/proc/<pid>/environ`, which succeeds in a shell but returns EACCES
under the server's `ProtectSystem=strict` sandbox — so `enumerate_cc_slots()`
returned 0 in-server and **three** features shipped green but inert since
July; the module's docstring claim "same-uid reads succeed" was a shell-tested
falsehood. The fix routed around the ptrace-gated read entirely.)

### Acceptance Bar + Measured Rate — the primary methodology

**Use this as often as it applies. It is the default way to build anything here,
not a special-occasion technique.** Unit tests prove the code does what you wrote.
This proves the thing actually WORKS — with numbers and a denominator.

Two artifacts, both produced BEFORE shipping:

**1. The acceptance bar — replay the real defect.** Take the actual failure that
motivated the work and run it through the new thing. If it does not catch/fix
the case it exists for, it does not ship, however elegant it is and however
green the suite is. Reconstruct the real case (from git history, from the
transcript, from live data) rather than a stylised approximation — a synthetic
case can pass while the real shape does not.

**2. The measured rate — run it against real data and produce a number.**
A claim like "low false-positive rate" or "it should be fine in practice" is not
evidence. Run the thing over a real corpus — recent commits, live rows, real
traffic, historical transcripts — and report `k/N (x.x%)`. **A number without a
denominator is not a measurement.** Then look at the individual hits and say
honestly which are real signal and which are noise; a "false-positive rate" that
turns out to be mostly true positives is a fire rate, and saying so is part of
the result.

The measurement is a GATE, not a footnote. Decide the acceptable threshold
BEFORE measuring, and if the number misses it, tighten and re-measure rather
than shipping with a caveat. When you tighten, re-run the acceptance bar in the
same breath — a filter that improves the rate by breaking the thing you built it
for has made it worse, and only running both together catches that.

Worked example (2026-08-27/28, an orphaned-literal detector). The tool itself
was still on an unmerged branch when this was written, so treat the first two
bullets as a method illustration; the figures in the paragraph after them are
derived from this repository's own history and can be re-derived.
- Acceptance: replayed a real review defect; the detector named the exact
  sibling file. PASS.
- Measured, false-positive side: `1/151 real file-edits fired (0.7%)`. The one
  hit was an identifier rather than prose, so the filter was tightened to
  require interior whitespace, re-measured at `0/151 (0.0%)`, **and the
  acceptance replay was re-run to confirm the tightening had not blinded it.**

**That example then became a lesson against itself, which is why it is kept.**
Everything above measures FALSE POSITIVES, and `0/151` reads as though the
tightening were free. It was not. Measuring the other direction needs
INDEPENDENT ground truth rather than the tool's own criterion — otherwise you
grade the tool on its own definition of success. Here that meant mining history
for a literal removed from one file and then removed AGAIN from a second file in
a later commit: the repo itself recording that the first fix left a sibling.
Over 1,584 commits that yields **95 verified cases (6.0% of commits)** — and the
interior-whitespace filter that scored so well on precision **excludes 48 of
those 95 (51%) by construction.** The number that actually decided the design
was recall against a budget cap: **28/47 in-scope cases caught (60%)** with a
cap of 6 literals per edit, versus **47/47 (100%)** with the cap lifted — every
one of the 19 misses was that single cap, and lifting it recovered all of them.

The rule that generalises: **a rate measured on one side of a tradeoff is half a
measurement.** A precision number with no recall number cannot distinguish a
good filter from a blind one, and the side you did not measure is the side that
will be wrong. Decide which direction matters for the thing you are building,
and measure that one first.

This also catches a specific self-deception. A first prototype of that same
detector used a regex and reported "2 findings" while **silently skipping the
entire class it was built for** (the pattern excluded backslashes; every prompt
string ends in `\n`). The acceptance replay is what exposed it. A matcher that
finds nothing is indistinguishable from a matcher that looks at nothing —
only replaying a known-positive tells them apart.

### Select, don't amputate — truncation is the absence of a decision

**Scope first, because bounding is often correct.** What makes something an
amputation is LOSS — the value cut here was the only copy. Nothing else. A
bounded PREVIEW of something stored intact elsewhere is a selection, and stays
one even if its handle is useless. Bounding against a hard external budget is
likewise correct: a hook's stdout cap, a context window, a column whose limit is
actually ENFORCED. That last qualifier is load-bearing here — this repo's SQLite
`TEXT` columns enforce no length at all, so "the database column" does not
excuse a cut; a self-imposed storage assumption is a decision to justify, not a
budget to obey. So is refusing an oversized value outright.

**And a SAFETY cap may be lossy — that is the one place cutting the only copy is
right.** An unbounded stream has no other copy by definition, and reading it to
the end to avoid "truncating" is how a runaway command exhausts memory; this repo
bounds subprocess output at a few MiB for exactly that reason
(`autonomy/executor/deterministic.py` `_read_limited`, whose own comment names
the `yes`-command threat; verified 2026-09-04). Losing the tail of
a log beats losing the process. The obligation there is not to keep the bytes, it
is to be LOUD about the cut — say the output was bounded and roughly by how much,
so nobody reads a clipped log as a complete one. A silent lossy cap is still the
defect; a declared one is a resource guard doing its job. This section is about
the remaining case.

**A handle that does not resolve is a separate defect, and do not conflate the
two** — that conflation is the mistake this section made about itself, twice.
Check the pointer, because a preview advertising a retrieval path that does not
exist teaches a lie; but when the full value survives somewhere, the fix is to
mend or drop the handle, never to remove the cap. Removing a cap to prevent a
loss that never happened is how this rule causes the damage it exists to stop.

**There, do not truncate.** Not strings, not lists, not context, not output.
Reaching for a character cap is a signal that a question was skipped, not
answered. Omitting is legitimate — it is a judgement about relevance. Truncating
is not: it is what happens when that judgement was never made, so the value gets
cut at a point that has nothing to do with meaning. A truncated value is
frequently worse than either alternative, because it still LOOKS complete, so
nobody checks it — at which point you may as well not have passed it at all.

Before bounding anything, answer: what is this value FOR, who reads it, why does
it need budgeting at all, and what actually breaks if it is unbounded? Solve
THAT. Usually the answer is "select less, whole" rather than "cut", and often the
bound turns out not to be load-bearing.

Three rules when a bound really is needed:

- **Bound by MEANING, not by one blanket number.** A closed set is validated
  against that set — a value outside it is INVALID, not "too long". A timestamp
  is a shape; half a timestamp is not a shorter timestamp. A blanket cap turns
  100,000 characters of foreign data into 300 characters of foreign data and
  calls it bounded.
  **This governs HOW you bound, never WHETHER you may.** Rejecting a structured
  value or a collection by size is correct and stays correct: an over-long id, an
  over-large batch or an implausibly large file is simply not a value we accept,
  and refusing it is a resource guard, not an amputation. What is forbidden is
  silently CUTTING them to fit. Only free text gets a bound it is expected to sit
  under; everything else gets one it must not cross.
- **Derive the number from the right thing, and record how.** A SAFETY bound —
  memory exhaustion, an abuse ceiling, untrusted input — does not come from the
  corpus at all: historical traffic says nothing about adversarial input,
  concurrency, or the memory you actually have, and a cap chosen from observed
  values will be exactly the wrong size when it matters. Derive those from the
  protocol, the capacity and the threat model FIRST, then use `k/N` only to price
  what the bound rejects. It is the COMPATIBILITY bounds — how long is this field
  in practice, what does this cap cost real readers — that a corpus answers, and
  for those: measure the real population and report `k/N` per the Acceptance Bar,
  then name the corpus, the query and the date. Naming them is necessary and not
  sufficient: the query is only re-derivable if the rows are still there when the
  next reader runs it, so a table with a retention window yields an EPHEMERAL
  observation. Say which one you have. And what the bound COSTS is a SECOND claim
  needing its own denominator — "the cap discards the part worth keeping" is
  exactly the sentence that sounds measured because it followed a measurement.
- **Omit explicitly, with a constant-bounded marker** (`<omitted: 104,823
  chars>`) in preference to a mid-value cut. An honest gap beats a
  plausible-looking fragment. The bound-plus-loud-flag half of this is already
  the house pattern; the character-count marker is a proposal, so do not go
  looking for a precedent that is not there.
  **Declaration is a requirement on top of the loss rules, never a substitute
  for them.** Stating the rule as "never cut" overshoots: this repo cuts
  mid-value in several places on purpose and is right to — a resource guard on
  an unbounded stream, a preview rendered next to the full record, a display
  string trimmed before escaping so the cut cannot land mid-entity. Each of
  those is a cut the loss rules PERMIT (a safety cap, a pointer-backed
  selection, third-party display text), and each is declared or bounded where
  it is read. The order matters: first the loss rules decide whether a cut may
  happen at all — announcing a sliced KEY does not un-merge the two identities
  it collapsed — and only then does declaration decide whether the permitted
  cut is honest. A silent permitted cut is still a defect; a loud forbidden cut
  is still forbidden.
  **When the total is not already known, say that instead of computing it.**
  Bounding a stream is the case: learning the exact discarded length means
  reading the whole source, which is the cost the bound existed to avoid. So
  `<omitted: ≥40,000 chars>` or `<omitted: rest of stream>` is the correct
  marker there, and demanding an exact figure would force either an unbounded
  read or an invented number — the two things this whole section is against.

One trap deserves naming, because it is what produced this rule: **a cap can
manufacture a correctness bug in the very data it was added to protect.**
Truncating an identifier used as a KEY merges two distinct identities into one,
and downstream code then attributes one subject's state to another. That is not
hypothetical — it shipped here. Two roster peers whose names shared a prefix
collapsed onto a single key, so one peer's success cleared the other peer's
recorded failure. A short DISPLAY handle is a different thing and is fine; the
rule is about the stored key, not the rendered one.

**A real need to truncate is a CONVERSATION to have, not a magic number to pick
alone.** If you catch yourself choosing 300 or 200 or 1000, stop and raise it.

**When there is nobody to raise it with** — an unattended background session —
the rule is NOT "never pick a number". It is **never pick one silently.** Bound
if you must, and put the reasoning beside the number, where a reader of the value
will see it. That includes the question that comes BEFORE the number — whether
this value needed bounding at all — because "is this big enough to matter?" is
the same judgement drawing on the same missing information, and skipping it is
precisely how the number gets invented. State both. A number with its reasoning
next to it can be argued with and corrected, which is all anyone needed from you.
What made the original defect dangerous was never that 300 existed — it was that
300 arrived silent, looking deliberate, and was then defended.

That default is about SIZE, never about secrecy, and the two must not be
confused. If a value may carry a credential, token, or personal data, the
unbounded default does NOT apply: fail closed, omit wholesale with a marker as
the "omit explicitly" rule says, and keep only non-sensitive metadata. Losing
diagnostic prose is recoverable; leaking a token is not.

**Then ask WHO WROTE IT as well as where it is going — two independent axes,
and each governs a different decision.** Destination governs DISCLOSURE: what
may cross a trust boundary is decided by where it lands, whoever wrote it — a
user-authored secret bound for an external channel still gets scanned and
quarantined. Authorship governs SANITIZATION: third-party-authored content
stays bounded and escaped no matter how trusted the destination is —
this repo truncates and HTML-escapes a stranger's email fields on the way to the
OWNER'S OWN chat (`outreach/engagement.py` `_sanitize_ping_field`, whose
docstring names both the threat and the destination; verified 2026-09-04),
because that channel renders HTML unescaped, so a display name someone else
chose would otherwise arrive as live markup in the one place the user trusts
absolutely. Maximally authorized destination, maximally defensive treatment.
Any rule that reads "it's going somewhere trusted, so pass it whole" deletes
that defence.

**And an authorized destination does not mean untreated.** The rule is narrow:
do not strip the USER'S OWN content on its way to the user. It is not a licence
to stop scrubbing on owner-facing surfaces, and this repo does not (each
verified 2026-09-04): it rewrites username-bearing paths out of what the owner's
own dashboard renders (`dashboard/routes/backup.py` `_scrub_reason`), gates
credential values behind an explicit reveal rather than showing them (the
References tab), scrubs the draft the user reviews so the copy they approve is
already clean (`content/egress.py` `should_gate`, `category == "content"`), and
in at least one subsystem deliberately keeps captured text out of its own
private store entirely (`attention/types.py`: "never stored";
`db/crud/attention.py`: "value-free … NO text"). "Into a private store" is
emphatically not a blanket exemption; some stores are built specifically to
never receive the value.

**One last thing, learned the hard way from this section itself.** Four review
rounds found defects in it, and every single one had the same shape: the cited
FACT was true, and the CONCLUSION drawn from it was false. A retrieval tool
really did lack an id lookup — and "therefore this is an amputation" was wrong,
because the value was stored whole elsewhere. A field really was written
unsliced — and "therefore it is a cheap place to record something" was wrong,
because reaching it aborted the whole run. Verifying that each cited fact is true
is the easy half, and it is not the half that fails. **State the inference as its
own claim, and check THAT.**

The sharpest instance is this section committing that error in the sentence next
to the one warning against it. A draft of the paragraph above cited a real file
that says, correctly, that a particular scrub applies to external audiences and
not to replies going to the user — and generalised it into "this repo leaves
owner-facing content untouched", which four other paths contradict. True fact,
false inference, two paragraphs from the rule forbidding exactly that. The
generalisation is the step to distrust, and it is seductive precisely when the
evidence under it is solid.

### A blocked compound command loses EVERYTHING in it

A PreToolUse block kills the **whole** Bash call, not the offending part — so a
guard firing on step 3 also silently discards steps 1 and 2, while the error
text talks only about step 3. This repo has many blocking guards
(`review_enforcement_commit`, `full_suite_guard`, `concurrent_test_guard`,
`git_push_guard`, the destructive/protected-path guards), so it is not rare.

**Never chain a state-changing step with a step that can be blocked.** Keep
`cd`, heredocs, file writes and restore-from-backup in their own invocation,
separate from test runs, commits, pushes, or anything a guard inspects.

**After any block, verify state before continuing.** Run `pwd`, and re-check the
file you believed you wrote. Do not assume the earlier half of the command ran.
Prefer `git -C <literal path>` over a persistent `cd`, so a lost `cd` cannot
silently redirect later commands. Do the same for scripts by spelling them
`$ROOT/scripts/…` (`ROOT="$(git rev-parse --show-toplevel)"`), which is what
`.claude/commands/deep-review.md` requires — a bare relative `scripts/…`
resolves against whatever the cwd drifted to.

**But the path is not what decides which worktree a script acts on. The PROCESS
CWD is.** Some scripts derive their target from the directory they run in —
`review_state.py`'s `evidence-path` and `mark` resolve the worktree via
`git rev-parse` with the inherited cwd, and never read `sys.argv[0]`. So run
those FROM the worktree they are about, in their own invocation, however you
spell the path. Get this wrong and review evidence is written under another
worktree's key, and the depth gate then blocks on a file you never wrote.

MEASURED 2026-08-28, with controls in both directions: from one cwd, the
relative, absolute and `$ROOT/`-prefixed spellings of `review_state.py` all
printed the SAME evidence path; the same absolute path run from three different
cwds printed three DIFFERENT ones. Path form: no effect. Cwd: decisive.

Measured cost in one session: four heredocs that never wrote, a
restore-from-backup that never ran (leaving a file deliberately regressed), and
a `cd` that never happened — so edits landed in the **wrong worktree** and had
to be reverted as cross-branch contamination. Guard false-positives make this
worse: `shell_parse` mis-parses backslash line-continuations and quoted heredoc
bodies, so legitimate commands get blocked too.

### Instance-Fix vs Class-Fix Gate

When a mechanism failed to write or propagate something (a memory, a
directive, a config row, a status flag), hand-writing the missing
artifact is a **data repair** — it mitigates ONE instance on ONE
install. It is never the fix. Before reporting anything as "fixed",
classify it:

- **Data repair** — you wrote the artifact the mechanism should have
  written. Label it "data repair" explicitly, and in the same session
  either fix the mechanism or get the user's explicit deferral
  (recorded as a follow-up). Never report a data repair as "fixed".
- **Class fix** — you changed the mechanism so the artifact is written
  correctly on every install, going forward, with a test proving it.

The test: "If a fresh install hits the same situation tomorrow, does my
change help them?" If the answer is no, you have repaired data, not
fixed anything. (Origin: 2026-07-17 — a stale-decision recurrence was
"fixed" with a hand-written memory + directive; the propagation
mechanism that failed to write them stayed broken.)

### Debugging Discipline (phase-gated)

Adapted from superpowers `systematic-debugging`. "Find root causes" is a value;
these are the GATES that make it enforceable:

- **Iron Law: no fix proposals until root-cause investigation completes.**
  Investigation (read the full error, reproduce, check recent changes, gather
  evidence) is a phase that FINISHES before any fix is proposed — never propose
  fixes in the same breath as the symptom. "It's probably X, let me fix that"
  = investigation skipped.
- **Fix-attempt cap: 3 failed fixes → STOP and question the architecture.**
  The debugging twin of the review escalation cap, with the same mechanics
  (count attempts visibly; the cap consumes standing approval). Each failed fix
  revealing a new problem in a different place is not bad luck — it is the
  signature of a wrong architecture or a wrong problem statement. Do not
  attempt fix #4; bring the pattern to the user.
- **Boundary instrumentation for multi-component failures.** When the path
  crosses components (hook → server → engine; CI → build → deploy), don't
  reason about where it breaks — LOG entry/exit at each boundary, run ONCE,
  and let the evidence localize the failing component before investigating it.
  (The "starved vs broken" check — inputs before regression-hunting — is the
  special case of this.)
- **Read the reference implementation COMPLETELY before deriving from it.**
  When a change must mirror what another subsystem does (what routing consumes,
  what the runtime resolves, what a protocol expects), read that subsystem's
  path END-TO-END first and derive from its own code/loader — never
  incrementally guess-and-patch toward it. Incremental spec discovery is how a
  review loop runs 7 rounds. (Origin: PR #1281 — the onboarding floor's key
  list was wrong three times until it was derived from the router's own
  `load_config` + call-site chains.)
- **Spec required-sets: enforce the WHOLE set at once, and lock it with ONE
  test.** The spec-facing corollary of the rule above. When a change must make
  code satisfy a canonical spec's *required-set* — a prompt's required JSON
  fields, a validator's mandatory keys, an allow-list — read the spec's required
  list in full and enforce ALL of it in one move; then write a single test
  asserting the required block == the complete canonical set (not one assertion
  per field). Adding only the field a reviewer just flagged leaves the next one
  for the next round, and a per-field test goes green while the next missing
  field ships. (Origin: PR #1333 — `_SALVAGE_PROMPT`'s Required block was
  completed one field at a time across three Codex rounds — cognitive_state_update
  → confidence → observations — for what `REFLECTION_DEEP.md` declares as one
  closed set `{observations, confidence, cognitive_state_update}`.)
- **Condition-based waiting.** When a fix or test must wait for a state change,
  poll the CONDITION (with a bounded deadline), never sleep an arbitrary
  duration — arbitrary sleeps are flaky under load and slow everywhere else.
  This complements the Timeout Policy (which governs the values).

### Test-First Discipline

Adapted from superpowers `test-driven-development`, scoped to where it pays:

- **Bug fixes: failing reproduction test FIRST — always.** Before touching the
  code, write the minimal test that reproduces the bug and WATCH IT FAIL for
  the expected reason. Then fix; the same test proves the fix and pins the
  regression. A repro test written after the fix proves nothing (it never
  caught the bug).
- **Verify-RED, always and everywhere.** Any new test must be seen to FAIL
  (correctly) at least once — via the bug, a reverted fix, or a deliberately
  broken assertion — before its green is trusted. A test that has only ever
  passed may be testing nothing; a whole suite passing every review round
  while a reviewer keeps finding real spec bugs is the tell that the tests
  encode the same wrong spec as the code.
- **A RED that comes back GREEN has AT LEAST six causes, and "the test is
  vacuous" is the LAST one to reach for.** In rough order of how often they
  actually occur:
  1. **The run never EXECUTED** — a guard refused it, a lock held it, the tool
     timed out — so there is no result at all. This is the CONFIDENT FALSE
     NEGATIVE: a sweep reporting "all mutations survived" is far more often a
     sweep that never ran. Make the runner ABORT when the test command emits no
     result line, and treat a SKIPPED/deselected line FOR THE TEST UNDER
     VERIFICATION the same way — it is a result line, and it still means
     nothing ran. Scope that check to the target: a suite carrying legitimate
     `skipif` tests emits SKIPPED lines on every healthy run, so a runner that
     aborts on ANY of them refuses every run — and the agent then either sits
     blocked or starts stripping skip markers to unblock itself.
  2. **The MUTATION silently failed to apply** — the auto-formatter reflows
     lines and a `str.replace()` anchor written from memory then matches nothing.
  3. **The test ran against a DIFFERENT COPY of the code** — an installed
     package shadowing the source tree, a stale `.pyc`, the wrong virtualenv, or
     (measured, this session) a path relative to a process whose cwd had moved to
     another worktree. The mutation applied, the run happened, the test is sound,
     and none of the other causes fits.
  4. **The mutation was BEHAVIOURALLY NULL** — it applied and parses, so every
     postcondition below passes, but it changed no behaviour: swapped operands
     that commute, an edit inside a dead branch, a type annotation Python does
     not enforce (also measured this session). The remedy is a different
     MUTATION, not a different test.
  5. **A SIBLING LAYER still enforces the invariant**, so the green is correct.
     When two layers produce the same behaviour, mutate the WHOLE mechanism, not
     one of its halves. (The duplicated-layer anecdote in the review-loop section
     is the worked example.)
  6. **A SIBLING TEST LEAKED STATE that masks the mutation** — an undone
     `monkeypatch`, a stray `os.environ` entry, a mutated singleton or a
     module-level cache — so the mutated path is never reached in THIS run.
     It matches none of the five above: the run executed, the mutation applied
     and is not behaviourally null, the code is the right copy, and no
     production layer is enforcing anything. MEASURED in this repo: a leaked
     disable lever made the very lock under test a no-op, and five real
     failures read as a story about the mechanism instead. The remedy is test
     ISOLATION (an autouse fixture that clears the lever) — NOT a different
     mutation, and NOT a different test.
  Only after all six: the test is vacuous. The list is ordered and still not
  closed — if none of them fits, the vacuous conclusion is UNPROVEN rather than
  established: look for the cause you have not modelled before rewriting a test
  that may be sound, because rewriting a sound test is the expensive mistake
  here.
  The PRINCIPLE, which is what to remember:
  **every injection must prove it applied, by its own postcondition, before any
  result is read as RED.** Two corollaries follow, and both have bitten:
  prove it against the value THAT injection was handed, never against the
  pristine original — with two or more edits, the first edit keeps a whole-file
  `mutated != original` true while a later one silently misses its anchor, so
  the partial mutation reads as complete (assert the anchor matched the expected
  number of times); and prove it with the mutated file's OWN parser —
  `compile(src, path, "exec")` for Python (NOT `ast.parse`: that only builds a
  tree, so it accepts context-invalid constructs like a `return` moved outside a
  function or a `break` outside a loop, and the `SyntaxError` then surfaces at
  COLLECTION, where a nonzero exit reads as a successful RED), `bash -n` for
  shell — since an invalid mutation breaks collection and reads as a successful
  RED, while the wrong language's parser
  rejects a valid mutation and hides a real survivor. Restore from a file copy
  taken beforehand, never `git checkout` — the work is uncommitted — and make
  the restore ATTEMPT unconditional ON THE RUN'S OUTCOME (`trap restore EXIT`, a
  `finally:`), never the tail of an `&&` chain. Unconditional means it always
  RUNS, not that it always OVERWRITES: what it writes is still gated on the hash
  check below. A `trap` that restores blindly is the very thing that destroys a
  concurrent edit. The expected outcome here is a NONZERO exit, and under
  `set -e` a trailing restore is exactly the statement that never runs (the
  `out=$(cmd)` entry in Common Traps is the same mechanism), so the shape that
  reads as careful leaves a deliberately-broken file in an uncommitted worktree
  on the ordinary path — as well as on interruption or a tool timeout. Take
  that copy ONCE for the whole sweep and refuse to start EACH cycle if the file
  already differs from it — a copy re-taken immediately before each mutation
  makes the check vacuous, since the file trivially matches a copy a moment
  old; the baseline exists to catch a PREVIOUS cycle that failed to restore, or
  a concurrent edit. Verify the restore by hash rather than assuming it. And
  restore ONLY what you broke: compare against
  the hash the MUTATION wrote before overwriting, because between the mutation
  and the handler another session, agent or formatter may have edited that file,
  and a blind snapshot restore silently destroys their uncommitted work — a
  final hash check does not catch this, it only confirms the overwrite
  succeeded. If the file no longer matches what the mutation wrote, PRESERVE it
  and report the conflict instead. The cleanest way to avoid the window entirely
  is to mutate inside an isolated worktree nobody else is editing.
- **Vacuous-test shapes to check for by name** (a list of the common ones, not a
  definition). The most frequent in practice is the one that never ran at all: a
  test SKIPPED by a marker, or deselected by a `-k` filter or a wrong path, which
  reports SKIPPED or "no tests ran" and never goes red — check the count, not just
  the absence of failures. Beyond that, a test is vacuous when: its
  assertion is ALSO true on the success path (`assert x.blocked is False` where
  a successful call also returns False — assert the fact that DISTINGUISHES
  them); its setup short-circuits the path it names (passing an explicit
  argument the code prefers over the env var under test); or its fixture never
  creates the shape it claims (a `bash -c 'sleep 30 # marker'` decoy
  exec-replaces itself and loses the marker from its argv — add a
  guard-the-guard assert that the fixture really has the property). Ask of every
  new test: *would this still pass if the mechanism it names were deleted?*
- **Contested/subtle specs: write the expectations first.** When what-should-
  happen is itself under discussion (which keys count, which states clear an
  alarm), enumerate the expectation table as failing tests BEFORE implementing
  — it forces the spec question to surface at design time instead of review
  round 4.
- **Corpus replay cannot find a false-POSITIVE class — generate the matrix.**
  Replaying real recorded inputs proves only that shapes you have ALREADY run
  still behave; it is structurally blind to a shape you have never typed. A
  guard change measured "0 false positives across 18k real commands" and still
  hard-blocked an ordinary command, because the corpus happened to contain no
  instance of the one shape that mattered: a construct the parser mis-handles
  that ALSO leaves the guard with no parsed segment. For any classifier
  or guard, enumerate the CROSS PRODUCT of the axes that actually drive the
  decision — {operation} × {the constructs your parser can mis-segment} ×
  {evidence present, absent} × {interactive, unattended} — and assert the invariant per
  cell, so an untested cell fails
  loudly instead of silently. Keep the corpus replay as the realism check; the
  generated matrix is the coverage check FOR THE MODEL YOU DECLARED, which is
  the most it can be — a construct you never thought of has no cell to skip and
  so passes in silence, which is the same false confidence one level up. Naming
  it "the coverage check" without that qualifier is the trap. Discovering new
  axes is a different instrument: differential or property testing against a
  canonical parser, which fails on shapes nobody enumerated. Skip a cell only
  with an explicit reason recorded in the skip, since a silent skip and a hole
  look identical.
- **Anti-patterns (binding):** never assert on a mock's behavior when the real
  code path can run; never add test-only methods/branches to production
  classes; fakes implement the real contract (real method names, real return
  types — import them). Test setup so complex it needs its own debugging =
  the design is too coupled; fix the design.

### Code Intelligence — pick the right lane

**Serena (Python LSP) is always live** — it parses current files per query, so
it's the default for symbol/reference/impact questions ("who calls X", "what
breaks if I change Z") and never goes stale. CBM gives the architecture/graph
overview. GitNexus does what neither can — multi-hop blast radius, execution
flows, route/tool maps, coupling/community analysis — but it is **snapshot-
based**: its answers are only correct when the index matches the working tree,
and it drifts after you pull merged PRs (its reindex fires on local commit, not
on pull). So reach for GitNexus deliberately for its unique views, and run
**`gitnexus analyze` first** when freshness matters; for live "who calls this"
during active editing, prefer Serena. There is no "always run impact before
every edit" mandate — that just gates work behind a tool that's stale-by-design.

- **Blast radius / impact:** Serena `find_referencing_symbols` (live) for the
  direct caller set; GitNexus `impact <symbol>` (reindex first) for multi-hop +
  affected processes/risk. Use the full UID if ambiguous
  (`Method:path/file.py:Class.method#N`).
- **Unfamiliar code:** `gitnexus context <symbol>` or browse
  `gitnexus://repo/GENesis-AGI/processes` (when fresh).
- **Custom questions:** `gitnexus cypher` — LadybugDB uses `CodeRelation` with a
  `type` property for edges, not Neo4j-style named edge labels.

Full syntax and Cypher examples: `.claude/docs/code-intelligence-guide.md`;
tool-selection decision matrix: `.claude/docs/code-intelligence.md`

### Common Traps

- **Fail-closed data access.** A data-access boundary must RAISE (or return a
  clearly-typed "unknown/unavailable") on missing scope or an unavailable
  dependency — it must NEVER silently return the wrong data, the singleton's
  data, or an empty result that reads as "all clear". A monitoring/consistency
  check whose dependency (Qdrant, FTS, a remote) is down reports `unknown`, never
  `healthy`/`degraded` — a dependency outage that masquerades as data
  corruption (or as cleanliness) is worse than a loud error. Prefer a helper
  that raises over one that swallows (the `batch_retrieve_point_ids`-raises vs
  `batch_retrieve_vectors`-swallows split exists for exactly this). Origin: the
  home-anchored-DB reads that silently returned no data from an empty worktree
  path, and the memory-integrity checker (2026-07).
- **Ego sessions are ACTIVE.** `src/genesis/ego/` is live (v3.0a11).
  Two egos: user ego (CEO, Opus) and Genesis ego (COO, Sonnet). Both
  run on adaptive cadence via the awareness loop. Changes here are
  production changes.
- **DB path confusion.** `genesis.db` is at `~/genesis/data/genesis.db`,
  NOT `~/genesis/genesis.db`. Use `genesis.env.genesis_db_path()`.
- **Column names.** Use `db_schema` MCP before assuming column names.
  The DB has 60+ tables.
- **Signal collectors.** Phase 1 built stubs; Phase 6 replaced some with
  real implementations. Code that looks complete may not produce signals.
- **Capabilities manifest.** `~/.genesis/capabilities.json` is write-once
  at bootstrap, not dynamic. New capabilities need registration in
  `_CAPABILITY_DESCRIPTIONS` in `src/genesis/runtime/_capabilities.py`
  AND a bootstrap init step.
- **APScheduler IntervalTrigger resets on restart.** `IntervalTrigger`
  counts from server startup, not from last successful run. If the
  server restarts more frequently than the interval, the job never
  fires. Use `CronTrigger` for anything longer than a few hours.
  Bit us with `user_model_evolution` (48h interval, daily restarts).
- **Silent skips are banned (provision-or-surface).** A setup/resilience
  feature that gracefully skips on a missing prerequisite (a package, a
  host knob) must either PROVISION the prerequisite (bootstrap.sh /
  host-setup.sh / a guardian reconciler) or register an effective-fact in
  `infra_profile` that the awareness posture check
  (`awareness/loop.py::_check_infra_protection_posture`) reads — so an
  unprotected box raises a standing alert instead of staying silent. A
  graceful skip with neither = a box that runs unprotected with zero
  signal (a sibling install ran weeks without swap/systemd-oomd until a
  memory spike wedged it, 2026-07). Guardrail:
  `tests/test_awareness/test_infra_protection_posture.py`.
- **Modules are NEVER subsystems.** A capability *module*
  (`src/genesis/modules/**`, an external pluggable capability — "hands,
  not brain", see `modules/base.py`) is not an internal Genesis
  *subsystem* (memory, reflection, ego, triage, autonomy, sentinel).
  Module memory writes must **never** set a `source_subsystem` value —
  that tag means "internal decisional output, exclude from default
  recall", which is wrong for module output. This is enforced
  mechanically: any `.store()` under `modules/**` passing
  `source_subsystem` is a hard CI failure in
  `tests/test_memory/test_store_subsystem_coverage.py`, which also forces
  every new memory-writer to either tag itself or be explicitly
  classified as user-context. `_KNOWN_SUBSYSTEMS`
  (`memory/retrieval.py`) is the authoritative subsystem list; adding a
  module name to it is a category error.
- **Destructive data migrations must reconcile cross-store mirror fields.**
  When a cleanup/backfill deletes data in one store (e.g. Qdrant vectors) but
  another store mirrors that data's existence (e.g.
  `memory_metadata.embedding_status`), the delete MUST also fix the mirror
  field. A deleted vector left as `embedding_status='embedded'` is a field
  that *lies*, and that lie is not cosmetic if any code path *reads* it —
  `MemoryStore._mark_superseded` gates an `update_payload` on
  `embedding_status != 'fts5_only'` and would fire a doomed write on the
  now-deleted point. Before assuming a stale field is harmless, grep for its
  *reads*, not just its writes. (Bit us in the source_subsystem purge, #918;
  fixed by #921 Step 2c — reconcile tagged rows to `fts5_only`.)
- **`immutable=1` reads miss WAL-resident writes.** A read-only `sqlite3`
  connection opened with `file:...?immutable=1` reads only the main db file
  and ignores the `-wal`, so a change you JUST committed (still
  un-checkpointed) is *invisible* — you get a false-negative "the write
  didn't land." To verify a live write, use `?mode=ro` (WAL-aware) or query
  through the server/CRUD path; reserve `immutable=1` for historical
  read-only sampling where a little staleness is fine. (A reconcile UPDATE
  read clean under `mode=ro` but appeared unchanged under `immutable=1`.)
- **Never hand-roll `gh`/bash/CLI argv parsing inside a hook or security gate.**
  Re-implementing shell/`gh` command-line semantics by hand (regex, manual token
  walking) creates an effectively UNBOUNDED adversarial-divergence tail: a good
  reviewer (Codex especially) will keep surfacing real gaps from true semantics —
  `--repo`/`-R`/`-Rvalue`, `GH_REPO`, `cd`, `&&`, `||`, `--body-file -`, duplicate
  flags, URL-vs-branch targets, enterprise hosts, `--help`, nested `bash -c` — and
  each named fix ships the next round's bug. This is the mechanism behind the
  measured Codex review-loop (an internal analysis of recent review-looping PRs:
  first-round findings were real catchable bugs, but later rounds were dominated by
  fix-churn on the hand-rolled parser itself — no finite pre-push audit bounds that
  tail). The cure is architectural,
  not "review harder": bind atomically to the real tool (e.g. `gh pr merge
  --match-head-commit`) and use a canonical parser (`shlex`/`bashlex`) — never
  bespoke semantics. Choose the fail direction PER BOUNDARY by consequence: the
  shared parser must DEGRADE gracefully (fail-open, never crash — that is
  `shell_parse.py`'s stated contract), while each security-critical caller
  (merge/push authorization) treats an unparseable command as a block (fail-closed
  THERE). A parser-wide absolute fail-closed is wrong — it would deny legitimate
  uncommon commands without closing evasion paths. Same family as the
  canonical-parser lesson (regex→yaml, #1393). Loci today:
  `scripts/hooks/shell_parse.py` + `scripts/hooks/git_push_guard.py`.

  **The boundary of the class — read this BEFORE you reason yourself out of it.**
  The tar pit is NOT "who tokenizes the string." Delegating tokenization to
  `shell_parse` and asking git for repo state does NOT exempt a guard: if the
  guard's CORRECTNESS depends on modeling what a git command WILL DO — which
  flags force, which operands are paths vs refs, which modes destroy, which
  repo is targeted — it is argv→EFFECT mapping, and that mapping is the same
  unbounded open-set surface as raw string parsing. This was reasoned around
  once already (2026-08-23, PR #1432): the guard used the canonical tokenizer
  and probed live git state, the author concluded "so it's not hand-rolling,"
  and Codex returned **13 real findings (10 P1) — every one of them living in
  the argv→effect layer**. The first architect finding of that shape (a
  separated global value-flag bypass) was the CLASS signal and got
  instance-patched; the next round found the rest of the class. n=1 IS the
  signal: any reviewer finding that exposes a semantic-modeling gap in a guard
  means STOP and re-architect — never patch the named instance.

  **Decision test (verbatim, apply before shipping any guard):** could a git
  flag you've never heard of change your guard's verdict? If yes, your claim
  is open-set — redesign to closed-set token claims (exact-form whitelists /
  literal token blocks) or to RECOVERABILITY (snapshot-then-allow, where a
  miss degrades to the status quo instead of a broken guarantee). Do not ship
  the open-set version and plan to harden it later; the review loop IS the
  hardening loop, one bug per round, and it does not converge.

  **Three corollaries, each bought with a non-converging review loop.**

  **(a) NEVER normalize the command text before a blind-spot probe.** A probe
  whose whole job is "notice that this text is unparseable" must read the RAW
  command. Preprocessing it can only ever DELETE the evidence the probe exists
  to find. MEASURED: a normalization step added in good faith — to stop a class
  of ordinary command from prompting — turned that shape into a silent
  ALLOW on two independent guards. Ordinary, not adversarial: the shape was one
  a developer writes without thinking, and the command really executed (verified
  against a shimmed binary, so the proof was execution rather than parse). The
  normalizer removed the very evidence the guard keyed on, and its own model of
  the shell's comment syntax was narrower than the shell's, so it could delete
  executed code as well. It then turned out not to be load-bearing at all: every
  case cited to justify it was one where the parser already resolved the
  operation, so its branch never ran. The fix was a DELETION, and it closed both
  defects at once. When a guard loop will not converge, look for the component
  that MODELS shell semantics and remove it — refining it is the loop.

  Scope this to a probe. Normalizing before a *tokenizer whose tokens you are
  about to use* is a different act with a different fail direction, and a
  sibling guard does exactly that, deliberately. Before calling any such site an
  instance of this rule, the question to answer is whether the normalization
  CAN change the tokenizability verdict or hide a target — and that is a
  possibility question, so a count cannot answer it. Demand a STRUCTURAL
  argument. The model is already in the repo, at
  `scripts/hooks/destructive_command_guard.py:101-120`: its replacements delete a
  line continuation — which is what the shell does with it — or insert
  whitespace and separators; none introduces a quote or an escape, so none can
  corrupt the quote balance shlex decides on. (The one place the shell keeps a
  backslash-newline literally, inside single quotes, is an over-block the guard
  states rather than hides.) That is a `cannot` — but read the next paragraph
  before reusing it, because the version of this argument that stood here for
  weeks was WRONG in a way that shipped a live bypass.

  **What that earlier version got wrong, and why it is the sharpest example on
  this page.** It added: *"a backslash-newline is a continuation, not an escape
  the shell keeps."* That is true only when the backslash is itself unescaped —
  an ODD-length run. In an EVEN-length run every backslash is escaped by its
  neighbour, so the last one is a literal character and the newline after it is
  a REAL command separator. The guard folded it anyway, deleting the separator
  and gluing the next command's first word onto the previous token, so no `rm`
  token existed and a destructive command was ALLOWED — measured end-to-end
  through the live hook, exit 0 where the plain-newline control gave exit 2. The
  legacy regex net did not save it either: that fires only when tokenizing
  FAILED, and this tokenized fine, just wrongly. Fixed 2026-09-02 by folding
  only odd-length runs.

  Note precisely what was and was not at fault, because the first attempt at
  this correction got it wrong in an instructive way: it said the quote-balance
  reasoning "was CORRECT and is not what broke", and exonerated it. Quote
  corruption is indeed not the *mechanism* of the token-glue bypass — but the
  quote-balance `cannot` **was not sound either, and it failed in the very same
  cell**. Deleting one backslash from an even-length run leaves an ODD run whose
  survivor escapes the next character; when that character is a quote, the fold
  DOES introduce an escape and shlex's balance shifts. Measured: it turned
  parseable commands UNPARSEABLE (11 of 20,000 random parseable commands under
  the old fold, 0 under the fix), which dropped them into the legacy-regex net —
  a net that matches neither `-Rf` nor `-r -f` nor `--recursive --force` nor a
  quoted `'rm'`, so they failed OPEN. One quiet universal quantifier (*every*
  backslash-newline is a continuation) falsified BOTH claims at once, and the
  old code therefore had two bypass families rather than one. The fold now
  always leaves an even-length run, which is what finally makes the `cannot`
  true.

  A structural `cannot` is only as good as the case-split it rests on, so state
  the split explicitly and enumerate it — the same trap as the direction-claim
  two paragraphs down, which was true of the operand and false of the option
  token. That both the wrong premise AND the first correction of it were written
  in a document teaching this exact discipline is the point: the danger is not
  knowing the rule, it is believing you already applied it.

  A corpus run only ever yields `did not, here`, and by the rule above it is
  corpus run only ever yields `did not, here`, and by the rule above it is
  structurally blind to the shape nobody typed. Run the corpus as
  corroboration, never as the proof, and pair it with a control that DOES flip
  — an unflipped corpus and an inert measurement look identical.

  State the direction too, not just a total — and then check the direction on
  the token you did not think of. The same sibling's fold used to split a word
  the shell joins, and the paragraph that stood here said the verdict could
  therefore only move toward refusing. That was measured on path operands and
  was false on the option token: the split could hide the flags, and a spelling
  the shell runs as a recursive-force removal of a protected path was allowed.
  A reviewer found it from the diff; the earlier audit had split every position
  of a path and never the option, and its zero came from benign traffic with no
  true-positive control. The fold now deletes the sequence, which is what the
  shell does, and the guard is asserted to give the same answer for the
  continued and the joined spelling. The general lesson is the one above: the
  examples you enumerate are a sample, and a direction claim needs the cell that
  would falsify it.

  Deliberately stated without the triggering shapes. A guard's defeat
  conditions are not a teaching aid, and this file is public.

  **(b) When the parse is unreliable, ASK — do not BLOCK.** A hard block forces
  a surgically precise trigger, and precision is exactly what an unreliable
  parse cannot deliver. Measured over five review rounds: every narrowing
  conjunct became a new way to STARVE the trigger (an over-strip that ate the
  evidence; a decoy segment that stood the net down), while every widening
  hard-blocked benign shapes (`git status # don't commit yet` was refused).
  Emitting a PreToolUse `ask` inverts the cost of being wrong — a false
  positive is one confirmation, a miss is the pre-existing status quo — which
  is what lets the trigger stay broad instead of clever. Measured prompt rate
  after widening: 0.43% of ~19k real commands.

  This does NOT loosen the fail-closed mandate above, and the two are easy to
  read as contradicting each other. Rule (b) is scoped to the git-operation
  blind-spot net — a guard whose trigger is deliberately broad and whose false
  positive is one confirmation. It is NOT a template for every guard: the
  protected-paths and destructive-command guards hard-block an unreliable parse
  even when a person is present, by design, because their false negative is an
  irreplaceable path or a broad recursive removal and their false positive is a
  rewrite. Within the net, the two rules are scoped by who is present: `ask` is
  the interactive form of the refusal, and where a session is unattended
  fail-closed governs and (c) applies. The operation proceeds unverified in
  neither case.

  This is the shipped shape now, not an aspiration, and one distinction inside
  it must stay visible. The shared parser still degrades to a naive split with
  NO failure signal, so a caller that only asks "did I get a matching segment?"
  allows. What closes the hole is a separate conjunction AT THE CALLER — no
  matching segment, AND the raw text is un-tokenizable, AND it names a gated
  operation — which yields `ask` interactively and a refusal when unattended.
  The parser's contract did not change and must not: it degrades, the caller
  chooses the fail direction, exactly as the mandate above requires.

  Earlier revisions of this passage described that net in the present tense
  while it was still unmerged. Both directions of that error are worth naming,
  because fixing one produces the other: an unbuilt mechanism written as
  shipped, and then — once it does ship — a hedge left standing that now
  understates the tree. A status sentence in a durable document is a claim with
  a date on it. Re-check it against the code whenever the surrounding work
  lands, not only when it is first written.

  Corollaries of the corollaries, each measured: a net that returns inline
  PRE-EMPTS every gate below it (a hard block with no other backstop was
  observed downgrading to a prompt) — set a reason and DEFER it to the tail
  where the other decisions are resolved. And an invariant pair of the form
  "never silently allowed" + "never hard blocked" is satisfied BY a block→ask
  downgrade, so pin the verdict EXACTLY wherever a hard block is the contract.
  Read "never hard blocked" here only as the shape of the trap — as an actual
  invariant it is false unscoped, and (c) below replaces it.

  **(c) The ask-cost argument does NOT survive the move to a refusal.** Rule (b)
  buys its broad trigger with "a false positive costs one confirmation" — and
  that is true only where someone can confirm. An unattended session has nobody
  to answer, so the obvious completion of (b) is a deny leg for that path, and
  that is where it goes wrong: the SAME broad predicate whose errors were cheap
  now produces unappealable refusals of ordinary work. MEASURED: sharing one
  predicate across both legs refused routine, entirely benign commands in
  unattended sessions, in the one failure direction the design had been chosen
  to avoid.

  Narrowing the refusal predicate is the obvious repair and it does not
  converge, for a reason worth stating precisely, because the imprecise version
  of it is false. The claim is NOT that no raw-text rule could ever work — the
  raw text does carry quote and comment syntax, and a complete canonical parser
  could read it, which is what the canonical-parser rule above tells you to
  reach for. The claim is bounded to a predicate built on the SAME degraded
  parse that failed: at that point the guard cannot say whether an occurrence of
  a gated verb is executed or merely quoted, commented, or documented, and that
  inability is the premise of the net existing. A predicate with no more
  information than the failure itself cannot both refuse the hidden operation
  and permit the inert mention. Escaping that needs a different information
  source — a fuller parser, or enforcement at the execution boundary, where
  mentions are never classified at all — not a cleverer rule over the same text.

  Three narrowing rounds on one predicate is the signature to STOP and make the
  policy decision explicitly. The decision taken on the guard this was learned
  from, and since shipped: the unattended path KEEPS refusing,
  and the invariant gets scoped rather than deleted. "A benign shape is never
  hard blocked" was simply false as written; what is true and testable is that
  it is never hard blocked where a human can approve, and where no one can, the
  refusal carries an ACTIONABLE stderr — the cause, plus a route that gives the
  gate MORE information rather than less.

  That last qualifier is load-bearing and the sloppy version of this sentence is
  a bypass instruction. Telling an operator to find "a rephrasing that avoids
  the predicate" invites mutating raw text until a degraded predicate stops
  matching, while the same unverified operation still runs — the fail-closed
  mandate defeated by its own error message. Only two routes are legitimate, and
  neither is an evasion. If the text is PROSE that merely mentions a gated verb,
  take it out of a shell command altogether — write the file with an editor tool
  — because it was never an operation to gate and never should have been parsed
  as one. If it IS the operation, express it so the parser can actually read it,
  which does not dodge the gate but submits to it. A rewrite that suppresses the
  trigger while still performing the operation is the one thing such a message
  must never suggest, and a message is not "actionable" if that is what it
  teaches. (Actionability here is a courtesy to the operator, NOT the
  RECOVERABILITY of the Decision test above, which is about a MISS degrading to
  the status quo. Same word, opposite failure direction; do not satisfy the
  Decision test by printing a nicer error.) State the narrower invariant; do not
  leave the false one standing, and do not delete the guarantee that still holds.

  Two things keep this from reading as licence. The open-set imprecision is
  tolerable here ONLY because the verdict is `ask` or `deny` and never `allow` —
  an imprecise predicate that cannot authorize anything does not violate the
  Decision test, while the same predicate wired to an allow would. And a broad
  regex over raw text is admissible ONLY as a mention scan whose outcome is an
  `ask` where a person is present and a refusal where none is — never an allow;
  the moment it maps argv to an effect and authorizes on the result, it is the
  hand-rolled-parser tar pit the mandate above forbids.

  A measurement informed this rather than settling it, and it was afterwards
  WITHDRAWN — which is the more useful half of the story. The claim was that
  across a sample of unattended sessions the leg had never fired in either
  direction. It does not survive: the transcripts it counted no longer existed
  when someone went to re-derive it, and the sample was far too small to carry
  a word as strong as "never" even while they did. Absence in a small sample is
  the expected observation for a rare event, not evidence the event cannot
  happen; the honest reading is that the leg's rate is LOW, which is a different
  claim and a weaker one. What was actually being chosen was which promise the
  suite should make, not which incident to prevent — and that conclusion never
  rested on the number, which is why it outlived it.

  A note on every number in these three rules, including the ones above. They
  come from one operator's local session transcripts at one date, and no corpus
  or harness is checked in, so a reader cannot reproduce or falsify them — the
  differing denominators are different harvests, not one corpus quoted three
  ways. That is not a hypothetical weakness: one of them was withdrawn the
  first time anyone tried, for exactly that reason. Treat them as the scale at
  which something was observed, never as a published result. A rule that only
  holds at someone else's numbers is not a rule; each of these should stand on
  its stated mechanism alone — and if one ever seems to DEPEND on a figure,
  that dependency is the defect to fix, not the figure to defend.

- **`out=$(cmd)` under `set -e` swallows the failure path — and NO linter catches
  it.** An assignment whose value is a command substitution INHERITS that
  substitution's exit status, so under `set -euo pipefail` a bare
  `out=$(cmd)` followed by `rc=$?` **never reaches the `rc=$?`** when `cmd`
  fails: errexit fires first. Any error handling keyed on `rc` is dead code on
  exactly the path it was written for. Two properties make this vicious: it is
  invisible (the function just stops, printing nothing), and it hides behind
  call sites — `f || echo …` / `if ! f` disable errexit INSIDE the function, so
  the bug stays latent until someone writes the first bare call.
  **shellcheck 0.9.0 does not flag it at any severity, including `-o all`**
  (SC2155 is the *different* `local x=$(cmd)` declare-and-assign case; measured
  2026-08-27 — no other linter was tested, and a future shellcheck could add it).
  Always write `rc=0; out=$(cmd) || rc=$?` — but **declare `local` on its own
  line first**: `local out=$(cmd) || rc=$?` NEVER fires, because `local` is a
  command and the compound takes `local`'s status (0), not the substitution's.
  MEASURED: the split form yields `rc=100`, the inline form yields `rc=0` and the
  caller proceeds as if the command succeeded — strictly WORSE than the bug this
  entry describes, since it converts a loud abort into a silent false success.
  The same applies inside the error handler: with `set -o pipefail`, `x=$(… | grep -v … | tail -1)` aborts when
  `grep` matches nothing, so a `${x:-fallback}` default written for that very
  case never runs — guard it with `|| x=""`.
  Origin: `scripts/lib/cc_version.sh` (caught only by adversarial review), then
  three instances found in one `scripts/bootstrap.sh` function whose whole
  diagnostic block was unreachable. Pinned by
  `tests/test_scripts/test_bootstrap_guards.py::test_install_pkg_*`.

### Iterative-Refinement Discipline

AI refinement cycles degrade code they were asked to "improve" — validation
gets stripped, types relaxed, function scope widened. Published measurements
show vague improvement prompts degrade security fastest across iterations.
Three binding rules:

1. **Iterate with scoped, explicit prompts** ("fix the race in X by
   serializing on Y"), never "improve/clean up/make robust".
2. **Be security-explicit when touching validation, auth, or boundaries** —
   state what must not be weakened.
3. **Diff each refinement for what it REMOVED** (constraints, guards, type
   enforcement), not just what it added.

Full failure-mode taxonomy + ordered audit passes: `references/ai-code-audit.md`.

### Anti-Rationalization

These are excuses sessions use to skip discipline. If you catch yourself
thinking any of these, STOP — you are rationalizing a shortcut.

| Rationalization | Why it's wrong |
|---|---|
| "This is just a simple fix, no tests needed" | Simple fixes break complex systems. The Qdrant regression was a "simple fix." Write the test. |
| "I already know what this function does" | You haven't read the implementation. Docstrings lie. Read the actual code. |
| "Tests pass, so we're done" | Tests verify what they cover, not the outcome. Verify actual end-to-end behavior. |
| "I'll clean this up in the next commit" | Next commit never comes in autonomous sessions. Do it now or create a follow-up. |
| "This file is too large to read fully" | Read the relevant section. Partial reads lead to partial understanding and wrong fixes. |
| "The linter is happy, ship it" | Linters catch syntax, not logic. Clean lint with broken behavior is worse than a warning with correct behavior. |
| "This change is low-risk, no impact analysis needed" | Your confidence is based on what you know; checking callers reveals what you don't. Serena `find_referencing_symbols` is live — run it. For multi-hop blast radius, `gitnexus analyze` then `impact`. |
| "I can skip the worktree, I'll be quick" | Concurrent session safety exists because "quick" commits have destroyed work before. Always worktree. |
| "The error is transient, retry will fix it" | Diagnose first. Retrying a misdiagnosed error wastes tokens and masks root causes. |
| "I'll add the follow-up later" | Follow-ups not created in-session are lost. Create it now while context is fresh. |
| "I don't need a skill for this" | If a skill exists, use it. The using-superpowers Red Flags table exists for this exact rationalization. |
| "This review round is the same class, it doesn't really count" | For an EXTERNAL cross-model round, the counter decides, not you — a repeat-class external round still counts; update it every external cycle and STOP at the cap. (Internal same-model reviews are never rounds.) |
| "The user already said proceed, so I can keep looping" | The escalation/fix-attempt caps CONSUME standing approval. Round 4+ (or fix #4) on an old instruction is a violation, not obedience. |
| "I can read the summary instead of the source" | Summaries lose context. If you're about to change code, read the code, not the description of it. |
| "The missing data was the problem — I wrote it, so it's fixed" | The mechanism that failed to write it is the problem. Hand-written artifacts are data repair, not a fix (see Instance-Fix vs Class-Fix Gate). |
| "I'll just add the field the reviewer flagged" | A spec's required-set is closed — derive and enforce ALL of it at once, with one test locking the whole set, or the next round finds the next missing field (see Debugging Discipline: spec required-sets). |

### Code Discovery

Use the right tool for how you're exploring:

- **Architecture overview** — CBM `get_architecture(aspects=["overview"])`
- **Finding symbols** — CBM `search_graph(name_pattern="...")` or Serena `find_symbol`
- **Call tracing** — CBM `trace_path(function_name="...")` or Serena `find_referencing_symbols`
- **Impact / blast radius** — Serena `find_referencing_symbols` (live caller set); GitNexus `impact` (reindex first) for multi-hop + affected processes
- **Config/doc/non-code files** — Grep/Read directly

Full decision matrix: `.claude/docs/code-intelligence.md`

### Auditing Existing Capabilities — enumerate, don't spot-check

Before claiming Genesis "lacks X", "needs to add X", or is "weaker than
<external system> at X" — or before any competitive/architecture comparison —
verify by ENUMERATION, not a spot-check. **Auditing a symbol is not auditing the
stack**, and a negative from a positive search is not evidence of absence:

1. Enumerate the subsystem's full module inventory before concluding anything is absent.
2. Trace the call graph BOTH directions — mechanisms often live in the
   wrapper/caller layer, not the first symbol (CRAG lives in the MCP recall
   wrapper, not `retrieval.py`; the reranker is applied by the caller).
3. Grep by CONCEPT with several synonyms, not one symbol.
4. Verify built/enabled/disabled against RUNTIME state (env gates, server logs),
   not code presence.
5. Multi-path systems → coverage matrix (N entry points × M mechanisms); hot
   auto-fired paths often carry a thinner stack than the deep path — a gradient,
   not an absence.
6. Confidence is capped by enumeration completeness.

A 2026-06-30 competitive audit wrongly claimed Genesis lacked CRAG,
scope-before-rank, and a live reranker — all three had already shipped. Full
protocol: procedure `codebase_audit` / CC memory `audit-enumerate-not-spotcheck`.
For "does Genesis already have X", consult the subsystem map
(`docs/architecture/CURRENT.md`, via the `subsystem-map` skill) FIRST;
`references/codebase-map.md` stays the package-level structural companion.

## Adaptive Review Protocol

Choose the review level proportional to the change:

| Change type | Review level | Examples |
|---|---|---|
| Docs / text / comments | **None** | Markdown prose, inline comments |
| Simple mechanical | **None** | Variable rename, typo fix, import reorder |
| Small focused fix | **Code-reviewer agent inline** | Single-function bug fix, config tweak |
| Substantial change | **Code-reviewer inline + `/deep-review`** | Multi-file refactor, new MCP tool, wiring |
| Prompt / LLM behavior | **Both + extra scrutiny** | System prompts, skill instructions, routing |

Decision criteria when ambiguous: "If the change could break a runtime
path not covered by its own unit test, it needs `/deep-review`. If it only
touches things with clear, isolated test coverage, code-reviewer inline
is sufficient."

**`/review` is not in this repo — it comes from the optional `superpowers`
plugin.** Where that plugin is installed, `/review` and
`superpowers:code-reviewer` are the preferred path. Where it is not, those names
do not resolve, and older text saying "Run /review" sends you after nothing —
which is why the enforcement hooks now name the plugin as optional rather than
assuming it. **Check, don't assume, in either direction**: the plugin is offered
by the official marketplace, so its presence is per-install, not per-repo.

Always available, plugin or no: **`/deep-review`** (dispatches the adversarial
pass AND writes the evidence marker), the built-in `/code-review`, and the
by-hand `scripts/review_state.py evidence-path` → `mark` flow.
`/audit-changes` is a light self-check, not a substitute for any of them.

The enforcement hooks (`review_enforcement_prompt.py`,
`review_enforcement_commit.py`) still fire on every change — they are
safety nets, not the decision-maker. This protocol provides the
judgment framework.

### Review depth is machine-checked (the front-stop)

The "substantial → adversarial /review-level audit" decision above is no longer
left to judgment alone (that judgment is exactly what failed on PR #1353 — an
under-depth inline pass was self-certified as sufficient). The commit gate now
COMPUTES substantiality from the staged diff
(`review_scope.classify_change_substantiality`) and BLOCKS a substantial change
whose review marker is not an ADVERSARIAL audit (`review_enforcement_commit.py`
Rule 2.5). Substantiality is a surface-area × risk model — ≥50 reviewable lines OR
>1 code file OR an auth/api/migrations file OR an **executable prompt/agent/skill
surface** (`.claude/agents|commands|skills/*`, `src/genesis/skills/*`), so the
"Prompt / LLM behavior → both + extra scrutiny" row above is machine-enforced (a
trivial edit to one is depth-audited). User-sovereign top-level CAPS docs
(`SOUL.md`/`USER.md`/`CLAUDE.md`) are exempt. Clearance binds the marker to the
reviewed diff's FULL content, so re-staging different content after the audit
re-blocks. A precision-filtered "no findings" inline pass is FALSE CONFIDENCE for a
substantial change — not clearance. Depth is override-exempt: a findings
`# review-override` does NOT waive it; only a loud, logged `# depth-ack` does (the
audited escape for a genuine format mismatch). "Adversarial" is verified
STRUCTURALLY — a severity ladder (BLOCKER/SHOULD-FIX/NOTE, CRITICAL/HIGH/LOW,
P1/P2/P3, or the CODE_AUDITOR JSON contract) + `file:line` engagement + substance —
so run the real recall-tuned audit (`genesis-architect` / `CODE_AUDITOR.md`); do not
hand-write a soft prompt.

**Honest enforcement model — VERIFY IT, do not assume it.** This section used to
claim the enforcing teeth were "the independent cloud reviewer + a required human
approval, gated by **branch protection**", and that the local hook was merely
advisory friction. **Measured on a live deploy (2026-08-27), that was false in
both halves**, and the correction matters because it inverts which layer you can
actually rely on:

- `GET /repos/{owner}/{repo}/branches/main/protection` → **404, not protected**.
  The protection the claim rested on did not exist. A *ruleset* did — a different
  API, invisible to the branch-protection endpoint.
- That ruleset required **one status check** (`test`), not the ten other blocking
  CI jobs. Lint, leak-detector and the rest were never server-side required.
- It carried `bypass_actors: [{actor_type: RepositoryRole, actor_id: 5 (admin),
  bypass_mode: always}]`. **A bypass entry voids the rule for that actor.** The
  sole author is the repo admin, and the merge command this skill mandates
  carries `--admin`.

⇒ For the actor who merges, the server-side backstop was **void**, and the local
`git_push_guard.py` merge gate was the *only* real enforcement — precisely the
layer the old text told you to discount.

**Two rules follow.**

1. **Never state where the teeth are without querying it.** Both endpoints, every
   time: `branches/main/protection` AND `rulesets` (then the ruleset by id, since
   the list view omits `bypass_actors` and `rules`). A rule with a matching bypass
   actor is decoration for that actor. A "required" check list is meaningless
   until you have compared it against the jobs that actually block.
2. **Do not weaken a local gate on the theory that something external catches it.**
   That reasoning is only as good as the configuration you just verified, and
   configuration drifts silently — nothing announces a ruleset edit.

The local gate's job is still to interrupt autopilot, and it is still editable by
the same author. But "editable in principle" is not "backed up in practice."
Where a real external backstop exists, name it and cite the query that proved it.

**Close every review with a verdict.** End each review with an explicit
`Ready to merge: Yes | No | With fixes` + a one-line reason, alongside the
DONE / DONE_WITH_CONCERNS completion status. And the DON'Ts that keep a review
honest: never "looks good" without reading the code; never a finding on code you
did not read; never vague ("improve error handling") — always `file:line` + why it
matters. (Deliberately NOT a "praise-first / acknowledge strengths" balance: an
adversarial audit's job is to assume bugs and enumerate the class, not to reassure.)

Two protocol steps apply to every review at "Code-reviewer inline" level or
above (full definitions in `.claude/agents/genesis-architect.md`):

- **Scope-drift check first**: compare stated intent (plan file / PR
  description / commit messages) against `git diff --stat` vs the merge-base,
  and open the review with the `Scope Check: CLEAN / DRIFT DETECTED /
  REQUIREMENTS MISSING` + Intent/Delivered block. Informational, never
  blocking.
- **Completion status last**: every review (and every skill workflow that
  concludes work) ends with exactly one of DONE / DONE_WITH_CONCERNS /
  BLOCKED / NEEDS_CONTEXT — with concerns listed, or blocker + what was
  tried, or exactly what context is missing. Findings use the
  BLOCKER / SHOULD-FIX / NOTE severity ladder with per-finding confidence
  and the pre-emit quote gate (a finding must quote its motivating
  file:line or be confidence-capped).

### Review-loop discipline

- **A review's findings are a SAMPLE, not a to-do list.** This is the single
  highest-value habit in this section, and the one most often skipped. CLAUDE.md
  already says to treat the *user's* examples as a sample and enumerate the
  broader class — the same rule applies to REVIEWER output and is easy to miss
  there, because N findings look exactly like an N-item work queue. Before
  fixing any finding: name the CLASS it belongs to, enumerate the full
  population of that class (programmatically — an AST walk or a grep that lists
  every member, not a mental scan), and fix the population. Then the next round
  has nothing of that class left to find.
  Origin: 2026-08-27, three defect-bearing rounds on one PR where each round's
  fix introduced the next round's defect — one of three renderers, then one of
  two branches in the same function, then one direction of a two-directional
  boundary. Every round I fixed exactly what was named. The enumeration that
  finally closed it took one AST script and would have worked in round one.
- **Fixing the named instance is how a loop runs away.** If two consecutive
  rounds each surface NEW defects, stop patching: that is the signature of
  instance-fixing, and the commit gate hard-blocks TWICE: first at the SECOND
  round (`review_enforcement_commit.py` mode-switch, cleared by `# audit-ack`),
  then at the third (escalation cap, `# escalation-ack`). Both call `_deny`, so
  the first stop arrives one round earlier than "the cap" suggests — see the
  two-tier table below. Switch to enumeration BEFORE the gate has to say so.
- **Run the pre-push adversarial pass with `/deep-review`** (`.claude/commands/deep-review.md`):
  one command that dispatches a fresh-context `genesis-architect` (+ `genesis-security-reviewer`
  on security surfaces) over the FULL branch diff with the right SHAPE — fail-open/state/TOCTOU/
  hand-rolled-parsing hunting, not a lint/secrets scan — and writes the evidence marker. This is
  the review that catches Round-1 bugs before Codex does; `/audit-changes` is only a light
  self-check.
- **PR review-findings status = `python3 scripts/hooks/git_push_guard.py --check-pr <N>`
  — ONLY.** This runs the SAME code path as the merge gate (strict fail-closed: a
  failed scan is never reported clean). NEVER hand-roll a `gh api pulls/N/comments`
  query to decide whether a PR is review-clean: a wrong filter's EMPTY result reads
  exactly like "clean". Origin (2026-08-23, #1431/#1432): Codex authors BOTH its inline
  findings AND its review-summary body as `chatgpt-codex-connector[bot]` — the REST
  `user.login`, WITH the `[bot]` suffix. A hand-rolled filter keyed on a DIFFERENT login
  (the GraphQL app login, which is not the REST login) matched nothing, and 13 real
  findings (10 P1) were reported to the user as "review-clean" until the merge gate
  blocked. An empty result from your own query is "my query found nothing", never "no
  findings exist". Freshness is a SEPARATE gate, and it is NOT a blanket
  reviewed-SHA-equals-HEAD rule: for a hook-surface or otherwise non-trivial delta a
  current Codex review must COVER head (reviews-API `commit_id == head`, or a clean Codex
  re-review comment naming head), but a trivial NON-hook delta may still merge on a stale
  review — `--check-pr` reports that as `codex-at-head : ok (STALE review of <sha>, delta
  since is trivial)`, a pass, not a block.
- **Hook-surface PRs merge only with a current GitHub Codex review — mechanical.**
  A PR touching the enforcement-hook surface (the guard code itself) gets no
  stale-review leniency: the merge gate (1) never classifies its post-review delta as
  "review-trivial", and (2) refuses `# stale-review-override` — regardless of Codex
  head-freshness — unless recorded fallback-review evidence exists for the EXACT
  base+head (`~/.genesis/override_review_evidence/<repo>__<pr>__<base12>__<sha>.txt`).
  A current at-head Codex review does NOT substitute for that evidence: the same sigil
  also waives `_check_base_is_default`, and the evidence identity binds the BASE tip,
  which a head-only review cannot vouch for (a hook-surface PR retargeted to a
  non-default base must be re-reviewed in that base's context). The surface is defined
  authoritatively by `_HOOK_SURFACE_PREFIXES` + `_HOOK_SURFACE_FILES` in
  `scripts/hooks/git_push_guard.py` (hook dirs, the global bash safety hook, the
  review-scope/state modules, hook wiring in `.claude/settings.json`, and the tracked
  configs the hooks read) — read those constants, kept exhaustive by the
  `TestWiredHooksFenceGuardrail` test, rather than any hand-copied list. The override
  procedure requires the user's explicit authorization, then a fallback adversarial
  review (local `codex exec` when quota allows, else genesis-architect), evidence
  recorded naming the head, then the merge re-run — the gate's block message walks
  through it.
- **One reviewer at a time — NEVER run two review agents simultaneously.** Run
  one reviewer (e.g. Codex), apply/verify its findings, then run the next
  reviewer (e.g. Claude) on the *fixed* code — sequential, never in parallel.
  The second reviewer should see the improved code, not the same unfixed diff
  both would otherwise review; parallel also doubles review spend per baseline.
  (Standing user directive.)
- **A different model is the real correctness gate; Codex is the default.** A Claude
  reviewer shares this model's blind spots, so it clears the LOCAL depth gate but is not
  the cross-model gate. When the GitHub Codex reviewer is unavailable AND the install
  has an approved alternative external reviewer — a DIFFERENT model, with explicit
  per-use user approval every time — run it non-interactively over the diff with an
  adversarial mandate. Which reviewer that is (if any) is install-local and belongs in
  user-level config, not here.
  **Unavailable is established by ASKING**: comment `@codex review`, wait, and read the
  reply. An explicit usage-limits comment is unavailability. Nothing else is — silence
  is not, and neither is `--check-pr` reporting no review, which says the same thing
  whether the reviewer is down or was simply never triggered at this head. Nor is a
  `codex exec` quota error: that is a separate surface on separate quota.
  Scope what you hand it exactly as `.claude/commands/deep-review.md` §1 specifies.
  **Verify it saw a diff at all**: a clean verdict that does not demonstrate WHAT it
  reviewed is void, and a false clean from the cross-model gate is worse than no review.
  Do not merge on a same-model-only review.
- **Escalation cap — a HARD BLOCK at 3 CROSS-MODEL rounds that each find NEW defects.**
  A *round* = one EXTERNAL cross-model review→fix→re-review iteration. INTERNAL
  same-model reviews (genesis-architect / genesis-security / any subagent) are NOT
  rounds — they never move the machine counter (see "THE COUNTER IS CROSS-MODEL ONLY"
  below) and must not be counted in the visible tally either, or a session re-creates the
  very false-stop this is meant to remove. A cloud-bot (Codex) re-review round counts; a
  local non-Anthropic reviewer (Kimi on .123) counts. The cap is enforced by three
  mechanics, not by vibes:
  1. **Visible round counter.** From the first EXTERNAL round, the plan file (or task
     list) carries `Cross-model rounds: N (cap 3)`, updated every external cycle. Rounds
     are a tracked artifact — "it's the same class, it doesn't really count" is exactly
     the rationalization the counter exists to kill (for a repeat EXTERNAL round).
  2. **The block point is BEFORE dispatching the next review.** The check is
     "am I about to trigger round 4+?" — evaluated at the mechanical moment
     (the `@codex review` comment, the re-push, the reviewer dispatch), never
     after reading the next batch of findings.
  3. **The cap CONSUMES standing approval.** A prior "proceed", "merge when
     clean", or "keep going until Codex is green" is VOID once the cap fires.
     Continuing a round-4+ loop on an earlier instruction is a violation, not
     obedience — STOP, post the round ledger (round → what it found → what it
     cost), name the cap explicitly ("we've hit the 3-round escalation cap"),
     and get a FRESH decision: keep hardening, switch to a robust-by-
     construction redesign, narrow scope, or shelve.
  **Tabulate findings by CLASS before fixing — but never let that change what
  COUNTS.**
  Tabulate the findings with a CLASS column before fixing ANY round's findings,
  including round ONE. Deferring the tabulation to the second round is what
  spends a round discovering a shared cause that was visible in the first: if the
  opening review returns several instances of one generator, an instance-level
  first pass fixes the ones named and ships the rest as the next round's
  findings. The tabulation is cheap and the round it saves is not. Findings that look unrelated one at a time routinely share one
  generator — five across three rounds once reduced to a single defect (two
  layers that had to agree about every lever and could not), and patching
  instances twice changed nothing while deleting the second layer removed all
  five at once. If one class has ≥2 entries, look for the shared GENERATOR and fix
  that; two findings can land in one superficial class without sharing a cause, so
  the count is the prompt to look, not the verdict.
  **Class grouping decides HOW you fix — never whether an external round counts.**
  The counter is deliberately class-blind: `bump_review_round` increments on a
  distinct staged diff and records no CLASS identity (it records only `source`, to
  gate internal-vs-external), and the marking rule below is finding-based — ANY new
  BLOCKER/SHOULD-FIX/P1/P2 from an EXTERNAL reviewer makes the round defect-bearing,
  including the second instance of a class you have already named. On an external
  round, passing `--clean` to keep a repeat-class round off the counter is a
  falsification, and worse than miscounting: an external `--clean` RESETS the streak
  to zero, so it disarms the cap outright rather than merely under-counting it.
  (Internal reviews never count either way, so there is nothing to falsify there —
  the temptation and the rule both live only on external marks.)

  A corollary that costs a round if missed: when you delete a duplicated layer,
  verify the class is closed by enumerating the registry for the BEHAVIOUR, not
  for the deleted file's NAME. A name-scoped guard beneath a class-scoped claim
  passes happily while a sibling oracle sits on the same event and matcher.
  (And if the invariant turns out not to be statically checkable, say so and
  narrow the test to what it can prove — a guard that cries wolf gets deleted by
  whoever hits it next.)

  These three are backstopped by a **machine layer** with **two tiers, not one**.
  The tier you hit FIRST is the one most sessions do not know exists:

  | Round | Gate | Demands | Sigil | Resets counter? |
  |---|---|---|---|---|
  | 2 (`cap-1`) | **MODE-SWITCH block** | Stop patching the named instance. Dispatch a FRESH-CONTEXT adversarial subagent over the ENTIRE diff; READ authoritative docs/source for any domain semantics; fix the whole enumerated CLASS in one commit. | `# audit-ack` | **No** |
  | 3 (`cap`) | **HARD STOP** | The full round-ledger stop above. | `# escalation-ack` | **Yes** |

  The round-2 block is not the round-3 cap arriving early — it is a different
  instruction. It says the *approach* is wrong (you are fixing instances, not the
  class), where the cap says *stop and re-decide*. Acking round 2 without actually
  doing the fresh-context audit is how a session arrives at round 3 having learned
  nothing. `# audit-ack` attests that the audit HAPPENED; it is not a "continue"
  button. Note the fresh-context subagent this tier mandates is INTERNAL — mark it
  plainly (`--source internal`, the default); it satisfies the depth gate and does
  NOT advance the counter, so it can never be the round that hard-blocks you. Only a
  repeat EXTERNAL (Codex/Kimi/…) non-convergence moves the streak toward the cap.
  (Origin, 2026-09-01: under the old model the audit this very tier demanded counted
  as round 3 and tripped the HARD cap — the gate penalized the remedy it mandated.)

  There is a separate depth tier that can fire at ANY round: a **substantial**
  change (≥50 reviewable lines OR >1 code file OR auth/api/migrations OR any
  prompt/agent/skill surface) whose marked review is not adversarial is blocked
  with `# depth-ack`. A `mark` that lands with no `file:line` engagement prints a
  WARNING and will be rejected by that gate — re-write the evidence with concrete
  anchors rather than acking past it.

  Backing all of it: `review_state.py` keeps a per-branch counter of
  CONSECUTIVE defect-bearing review rounds, and the commit gate
  (`review_enforcement_commit.py`) HARD-BLOCKS the commit at `ESCALATION_ROUND_CAP`
  (3) unless the command carries a deliberate trailing `# escalation-ack`.

  **THE COUNTER IS CROSS-MODEL ONLY.** The streak exists to catch *cross-model
  non-convergence* — an EXTERNAL reviewer finding NEW defects round after round. It does
  NOT exist to penalize the free, encouraged act of reviewing your own work. So `mark`
  takes a `--source {internal,external}` (default `internal`) that records WHO PRODUCED THE
  FINDINGS, and that is what decides whether the round counts.

  **EXTERNAL is judged by the reviewing MODEL, not the gateway/provider — this is the "big
  one".** External = a review by a non-ANTHROPIC model. Anthropic Claude via ANY route
  counts as INTERNAL — including a Claude model reached through OpenRouter (the repo's
  `openrouter-haiku/sonnet/opus` routes are Claude), so "it went through OpenRouter" NEVER
  makes a review external. And Genesis's OWN cognitive/routing systems are never reviewers:
  they are cognitive infrastructure, not a review service, so no internal Genesis model call
  is ever `--source external`. Approved external-review methods TODAY are **Codex** and
  **Kimi (on .123)**; **OpenRouter is NOT an approved method today** (a future option, not a
  current one). The rule below keys on this:
  - **`--source internal` (the default)** — a same-model self / genesis-architect /
    genesis-security / any-subagent review. It is free and shares the author-model's
    blind spots (rubber-stamp risk), so it **NEVER moves the streak** — not an
    increment, not a reset — whatever it found. No outcome flag is needed (a bare
    `python3 scripts/review_state.py mark --agent-output <path>` is a valid internal
    review). This is the fix for the weeks of false blocks: your own audits, including
    the one the round-2 mode-switch gate itself *mandates*, can never trip the cap.
  - **`--source external`** — a non-Anthropic cross-model reviewer drove this round.
    This is the ONLY kind that counts, so it REQUIRES exactly one outcome flag:
    - a NEW **BLOCKER / SHOULD-FIX / P1 / P2** → `--defects`
      (`… mark --agent-output <path> --source external --defects`) — the round counts.
    - **no** new BLOCKER/SHOULD-FIX/P1/P2 → `--clean`
      (`… --source external --clean`) — RESETS the streak (circuit-breaker
      reset-on-success). Only an EXTERNAL clean round resets; an internal "looks
      fine" can never reset a standing cross-model streak (that would be a
      self-rubber-stamp reset).

  `--source` describes the REVIEW THAT PRODUCED THE FINDINGS, not who typed the
  evidence file: a mark recording "verified + fixed Codex's (or Kimi's) findings" is
  `external`; a mark of your own architect/security audit is `internal`. A round is
  CLEAN iff the external review found no BLOCKER/SHOULD-FIX/P1/P2 (and no security
  CRITICAL/WARNING). NOTEs, nitpicks, and dispositioned optional-hardening do NOT make
  a round defect-bearing. The ack (`# audit-ack` / `# escalation-ack`) is a conscious,
  logged act (like `# review-override`); adding it — or falsely passing an external
  `--clean` — WITHOUT the honest review result is the same violation as ignoring the
  prose above. (Supersedes feea3f71/#1446: its unconditional required-outcome only
  ever bit internal re-audits, which now can't inflate the streak at all; the outcome
  requirement is kept where it is still load-bearing — on external marks.)
  Caveats: **multiple findings in a single pass = one round** (not an
  escalation); the same defect reappearing (an incomplete prior fix) is a
  fix-it-properly issue, not an escalation trigger. This complements the
  enumerate-class-then-lock convergence discipline — the cap is the escalation
  trigger when the class won't lock within ≤3 rounds. (Origin: PR #1281 ran ~7
  reviewer rounds because a standing "proceed once clean" silently carried
  through rounds 4–6.)

  **The Codex-round twin of the cap** (`git_push_guard.py`
  `_check_codex_round_escalation`): the local counter above is BLIND to a loop
  that churns through CODEX rounds while every local review is clean — the
  2026-08-12 MW-3 #1372 whack-a-mole shape (5 Codex rounds, local counter at 0,
  and the round-4 "fix" of a non-bug introduced the only genuine liveness bug).
  So `gh pr comment … "@codex review"` HARD-BLOCKS once the PR already carries
  `ESCALATION_ROUND_CAP` Codex reviews (counted live from the GitHub API;
  fail-open on any API error), until a trailing `# escalation-ack`. Before
  acking, DO THE STEP-BACK the block prints: (1) triage every open finding —
  {live bug | latent trap | hardening | observation}; only live bugs and
  cheaper-now-than-later traps may change already-reviewed code, the rest get a
  documented acceptance or route to the PR that owns the area; (2) fix
  MECHANISMS, not instances; (3) for state-machine/queue code, enumerate EVERY
  status value and trace the change under each (your tests encode your own
  state model — they can't catch states you didn't consider); (4) consider
  REVERTING a prior round's fix rather than patching it again; (5) escalate to
  the user with a minimize-change recommendation. The ack asserts that
  step-back happened — appending it without doing the work is the same
  violation as falsifying `--clean`.
- **External review feedback is a set of claims to VERIFY, not orders.** For
  every bot/external finding: check it against the actual code (its stated
  mechanism may be wrong even when the underlying concern is real — quote the
  disproving file:line), check whether the "fix" breaks existing behavior or
  violates YAGNI, and push back with technical reasoning when it's wrong for
  this codebase. A finding that conflicts with the user's prior design
  decisions (e.g. live network calls in a hot autonomy gate, weakening an
  approval gate) is a STOP-and-discuss, never an auto-fix. Chasing a reviewer's
  green checkmark with a change you believe is wrong is a discipline failure.
  No performative agreement — state the verified fix, or the reasoned pushback.
- **Waiving the review GATE is not waiving the FINDINGS.** When the user says
  "skip the review" for a trivial change, that waives the blocking *ceremony*
  (the gate and its `*-override` sigils — note `# review-override` is the one
  that waives the findings scan) — it NEVER licenses ignoring a reviewer's
  *substantive* findings. Read Codex's inline findings
  (even non-blocking P2s) BEFORE merging even when the gate is waived, and
  engage each on merits: verify it, then fix or consciously accept with a
  stated reason. Merging past unread findings on a "skip review" is a trust
  breach, not obedience. (Origin: #1439 merged past 3 correct Codex P2s.)

## The Gate Machinery — the sequence, and why it bites

Ten enforcement layers sit between a change and `main`. Learning them by hitting
them costs a session real time, every time. The canonical sequence:

```bash
python3 scripts/review_state.py evidence-path     # -> ~/.genesis/review_evidence/<key>.txt
# ... write the adversarial audit to exactly that path ...
git add <files>                                   # STAGE FIRST — mark hashes --cached
python3 scripts/review_state.py mark              # INTERNAL genesis-architect audit — plain mark, never counts
git commit -F <msg-file>                          # bare, not piped (see below)
# (a non-Anthropic cross-model round would instead be: mark --source external --defects|--clean)
git push                                          # approve the dialog on a branch's first push
gh pr create ...
gh pr comment <N> --body "@codex review"          # after EVERY subsequent push
python3 scripts/hooks/git_push_guard.py --check-pr <N>
gh pr merge <N> --squash --admin --match-head-commit <head>   # verbatim from --check-pr
```

**Ordering and lifetime rules that are not obvious:**

- **Stage → mark → commit.** `mark` hashes `git diff --cached`. Re-staging or
  amending after marking invalidates it, and the check fails CLOSED.
- **Evidence expires in 30 minutes.** The audit file must be recent when you
  `mark`, and the marker itself expires on the same clock. A long detour between
  audit and commit means re-marking.
- **`evidence-path` and `mark` key off the PROCESS cwd**, not a flag. Run them
  from the same worktree as the commit or the key diverges silently.
- **A successful commit WIPES the marker.** The next commit needs a fresh review;
  this is deliberate, not a bug.
- **`--match-head-commit` is mechanically required**, not a nicety — the merge
  arm blocks outright when a verified head exists and the flag is absent. Copy
  the whole command from `--check-pr` output rather than reconstructing it.

**Traps with a real cost, each one measured:**

- **A BLOCKED Bash call runs NOTHING** — including earlier `&&` segments and
  heredocs. If `mark && commit` is blocked, the `mark` did not happen either.
  Run gate-adjacent steps as separate calls.
- **Ack sigils bind per-guard, and mostly to the LAST pipeline segment.**
  `git commit ... | tail  # audit-ack` puts the ack on `tail`. Run the commit
  bare. Some guards accept a sigil on any segment, others only on the offending
  one — do not generalise from one guard's behaviour.
- **Sigils must lead the trailing comment.** The override is read from the
  leading run of recognised tokens, so `# see audit-ack notes` overrides nothing.
- **`--no-verify` is blocked before any override is even considered.** There is
  no way to skip the native hooks; fix the cause.
- **Chained commits are heavily restricted** — across worktrees, blocked
  outright; within one, later commits must be pure `--amend` with no intervening
  git command. `cd "$VAR" && git commit` fails closed with a *branch-verification*
  message, which reads like a branch problem and is not: use a literal path.
- **Worktree removal is not yours to do.** `git worktree remove` is blocked;
  `scripts/worktree_lifecycle.py` owns it, with a 7-day trash bin, and reaps
  unchanged worktrees on a daily timer. Leave a dead worktree alone.
- **Editing a tracked git hook blocks the commit** until its hash is re-recorded
  (`scripts/update_hook_versions.sh`), and editing `scripts/hooks/*` changes
  nothing until `sync-hooks.sh` copies it into `.git/hooks/`.
- **`.github/**` and prompt surfaces are never "docs"** for substantiality
  purposes — they always reach the depth gate.

**CI is not one check.** Ten of eleven jobs block; only `review-depth-check` is
advisory. Several are reproducible locally BEFORE pushing, which is far cheaper
than a red PR:

```bash
ruff check src/ tests/ scripts/
python scripts/check_external_io.py
python scripts/check_subsystem_map.py
python scripts/check_shared_artifact_consumers.py
python scripts/check_frozen_clock.py
```

Do NOT run the full pytest suite locally (it is banned, and the concurrent-test
guard blocks a second run anyway) — CI's `test` job is the blocking one.

**Detecting a running pytest: match argv STRUCTURE, never a substring.** Any
check that greps command lines for `pytest` matches ITS OWN command line —
`pgrep -f "python -m pytest"`, `ps | grep`, and a bash `case *pytest*` all
self-match, as does another session's wait-loop. Test `argv[0]`'s basename, or a
python interpreter with an adjacent `-m pytest`.

## Pre-Commit Gate

Verify before any commit:

- `git diff --cached --stat` — every file in the diff belongs to your work
- `git status --short` — check untracked files (should be staged or ignored)
- Review level applied matches the adaptive protocol above
- Staged files do not include secrets (`secrets.env`, `.env`, credentials)
- **New-Store Gate (anti-proliferation).** A new persistent store — a DB table, a
  Qdrant collection, a file-plane under `~/.genesis/` — needs a written
  justification for why an existing store cannot hold it, plus a note on how it
  stays consistent with related stores (and its retention + backup path). The
  memory subsystem already sprawls across ~9 logical systems / 3 physical planes
  because this gate did not exist; a table that "felt cleaner" is how store #10
  is born. Reuse an existing store or an existing convention (e.g. the
  `~/.genesis/eval/golden/` install-local golden-set convention, #1143) unless
  the justification is real. Prefer NOT reusing a store whose SEMANTICS differ
  (don't shoehorn a store-health row into the model-eval `eval_runs` table just
  because it is "a table that exists").
- **Private-data scan before every push (public repo).** Grep the ENTIRE diff
  (`git diff origin/main...HEAD`) for private/identifying data — real names,
  company/product names, emails, IPs, private career/project specifics, verbatim
  user messages. Check ALL surfaces, not just prose: **source comments,
  docstrings, and test fixtures/data** are the easy misses. Use a synthetic
  stand-in in tests, never the real private artifact. (2026-07-01: a verbatim
  private DM leaked via a test docstring + a code comment after the commit
  message and PR body were already clean.)
- GROUNDWORK-tagged code not accidentally deleted
- New capabilities registered in `_capabilities.py` + bootstrap manifest
- **Conventional commit prefixes**: `feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`. Scope optional: `feat(ego): add cadence manager`.
  Subject line under 72 characters. Dominant category wins if mixed.
- **NEVER push to main or merge into main without a PR and user approval.**
  Enforced by PreToolUse hook.
- **Targeted tests during development.** Run ONLY the relevant test file(s)
  for your changes. NEVER run the full test suite locally — CI handles that.
  Check CI via `gh pr checks`. Bare `pytest` without a file path is banned.
- **Commit continuously**: after every logical unit of work. Uncommitted = lost.
- **PR closes a ledger item → cite `Ledger: <item-id>` in the PR body** (the
  32-hex `session_ledger` row id, own line, e.g. `Ledger: 71337fab…`). The
  repo-pulse worker auto-absorbs the row with PR evidence at the next session
  boundary — deterministic, reversible via `session_ledger_update`. A bare id
  mention WITHOUT the `Ledger:` marker is context, not completion (the pulse
  only proposes it). Find ids via `session_charter` or the charter injection
  block.

## Generalizability Gate — build for ANY install, not this one

Genesis is a public, cloneable system. Every change must work on ANY user's
install, not just the machine it was written on. Standing user directive.

**Hardware/scale adaptivity.** Other installs have different RAM, disk, CPU
count, and workload scale. Never hardcode absolute resource numbers or scale
assumptions:

- Memory/disk caps: percentage-of-available or config-derived, never fixed
  GB (precedent: #1029 percentage-based memory caps). Concurrency: derive
  from `os.cpu_count()`/config, never a literal core count.
- Hard minimums are allowed but must be EXPLICIT (documented in install
  docs/config comments), not implicit assumptions that fail mysteriously.
- Workload scale varies (PR velocity, table sizes, transcript sizes):
  enumerate with pagination/bounds and LOUD truncation markers, never
  silent caps (precedent: repo-pulse `limit_hit`).
- Optional dependencies AND optional infrastructure (Ollama, GPU,
  individual API keys, a host VM/guardian, Tailscale, voice/edge hardware)
  must degrade gracefully behind detection/config — presence is never
  assumed (precedent: Ollama-optional, `API_KEY_VOYAGE`-gated reranker,
  guardian features no-op without `guardian_remote.yaml`).

**No install-specific values in code.** IPs, hostnames, usernames, absolute
`/home/<user>` paths, GitHub slugs, timezones: these belong in generated
local config (`~/.genesis/config/genesis.yaml`, written by
`setup-local-config.sh`) or config overlays — never in committed code,
defaults, or tests. Resolve repo paths via `genesis.env.repo_root()` /
`genesis_db_path()` (GENESIS_REPO_ROOT-aware); resolve GitHub slugs LIVE
(`gh repo view --json nameWithOwner`) — a configured slug can name a
real-but-wrong repo and return plausible stale data. Shipped config defaults
must work on a fresh install with ZERO overlay.

**Leak-detection patterns follow the same rule — never hardcode an install's
private literals into a tracked scanner.** A public repo's CI grep / gitleaks
rule / commit-msg hook / contribution sanitizer must ship only generic CLASS
patterns (all RFC1918, IPv6 ULA per RFC 4193, `/home/<user>` shapes — see
`scripts/check_portability.sh`). This install's SPECIFIC literals (its
hostnames, subnets, ULA prefixes, private repo name, timezone) live only in the
GENERATED `~/.genesis/release-fingerprints.txt` (built by
`genesis.contribution.fingerprints` at bootstrap; hand-edited section preserved,
backed up via `backup.sh`) and — opt-in — the public repo's
`GENESIS_PRIVATE_PATTERNS` Actions secret. A scanner that must exclude its own
definition files from scanning is self-allowlisting a leak. See procedure
`public_repo_leak_detection_design`.

**Tenant-neutral, not tenant-shaped.** Genesis is a single-user sovereign
system. Build clean single-user code; do NOT pre-genericise for multi-tenancy —
no `tenant_id` columns, ACL tables, or context objects that always resolve to
one identity — absent a committed multi-tenant requirement. Premature
genericisation taxes every change with abstraction for a customer that may never
exist, and the reliability work that WOULD precede multi-tenancy (a fail-closed
data boundary, consolidated stores, provenance) is worth doing on its own
single-user merits and makes the eventual retrofit easier as a side effect.
When that requirement lands, tenancy is a well-understood retrofit — not
insurance to carry now.

**Deploy-path answer required — "how does this reach other installs?"**
Every PR must have an answer for both an EXISTING install and a FRESH clone.
Merged-but-undeployable-elsewhere is a bug. The standard paths:

| Change type | Deploy path |
|---|---|
| Runtime code | `git pull` + server restart (update.sh does both) |
| DB schema | additive idempotent migration — applies at restart |
| One-off data fix / backfill | data-migration framework (post-boot, idempotent) — NEVER a hand-run script only this install executed |
| Config default | repo config file (+ optional local overlay); works with no overlay |
| systemd unit / timer | registered in bootstrap.sh AND the update path — never hand-`systemctl enable`d only here |
| Hooks / MCP servers | land at next CC session start (note the mid-window in the PR) |
| Guardian / host VM | `update.sh` redeploy (Host-Deploy Gate below) |

**When a change CANNOT deploy through the standard paths** (one-time host
action: packages, sudoers, cgroup settings, firmware), it must ship one of:
(a) a gated self-heal that reconciles on a recurring tick (precedent: the
guardian's swap reconcile — checks every tick, repairs config + live state,
opt-out flag), or (b) an explicit, documented operator step in CHANGELOG +
install docs. Silent "works here because I hand-fixed it" divergence is the
failure mode this gate exists to kill — it bites hardest on guardian/host
changes.

**Empty-state correctness — a fresh install is state zero.** Every feature
must behave correctly with NO accumulated state: empty tables, no history,
no cursor files, first run ever. First runs bound their own work
(precedent: repo-pulse `lookback_days` — never "all history"); readers of
possibly-absent tables degrade explicitly (precedents: dashboard
`charters_available: false`; charter injection byte-identical when the
migration hasn't applied yet). Test the zero state, not just the populated
one — "works here" often means "works with two years of accumulated state."

**External-tool version drift.** Other installs run different versions of
`gh`, GitNexus, Node, and Claude Code — and upgrade on their own schedule.
Never key logic on one version's observed behavior without a fallback:
prefer first-class config over output-patching, and keep the patch as a
safety net when older versions ignore the config (precedent: `.gitnexusrc`
+ the strip job for rc-unaware versions); parse external-tool output
fail-closed against the LIVE stream, never assumed semantics; pin versions
only where the system owns the pin (`cc_version.sh` + cc-align).

**A settings lever for every autonomous behavior.** Anything that acts
without a user in the loop — detached workers, scheduled jobs, auto-writes
— ships its operator lever in the SAME PR: a settings domain
(`off | propose_only | live` or equivalent) plus an env kill switch, with
invalid values degrading toward LESS write authority (precedents:
`repo_pulse` domain + `GENESIS_REPO_PULSE_DISABLED`;
`session_ledger_shadow` live-coerced to shadow). Another operator must be
able to turn your feature off — or cap its authority — without editing
code. This is "the user decides tradeoffs" applied to every install.

**Retention for every unbounded store.** Any table, log, or directory that
grows without bound ships its prune path in the SAME PR, wired into
`disk_hygiene.sh` or an existing retention tick (precedents: repo-pulse
45d prune; ledger-shadow 45d prune; label-aware attention-snapshot GC).
An unbounded store is a slow disk-leak on someone else's smaller disk —
retention is part of the feature, not a follow-up.

**Install-agnostic tests.** Tests must pass on a fresh clone with no
Genesis services, no live DB, no network, no `gh` auth, no local config:
synthetic fixtures only (never real usernames/slugs/IPs — doubles as the
privacy gate), injectable runners for external commands, `tmp_path` over
real paths, no wall-clock dependence. CI on GitHub's runners IS the
reference "different install" — anything a test can't exercise there needs
an injectable seam, not a skip-on-my-machine guard.

## Host-Deploy Gate (merged ≠ deployed)

A merged PR that touches host-deployed paths is **NOT done at merge**. The
guardian and the host VM only pick up changes when `scripts/update.sh` runs —
merging and walking away leaves the host running stale code indefinitely
(observed live: a host guardian sat 3 PRs behind for a week because every
session assumed deploy "happens somehow").

**Trigger paths** (match = this gate applies): `src/genesis/guardian/`,
`scripts/guardian-gateway.sh`, `scripts/install_guardian.sh`,
`scripts/host-setup.sh`, `scripts/update.sh`, `scripts/lib/cc_version.sh`.

**After merging such a PR, in the same session:**

1. Run `scripts/update.sh` from `~/genesis` (it redeploys the guardian when
   guardian-relevant paths changed and heals host/container CC + Node pin
   drift — including on a no-delta run).
2. Verify the deploy landed: gateway `version` op reports the expected
   `deployed_commit` / CC version; guardian tick healthy in its journal.
3. State the deploy + verification result explicitly in the wrap-up. If the
   deploy cannot happen this session (host unreachable), create a follow-up
   via `follow_up_create` — never leave deploy as an implicit assumption.

**The reverse direction is equally binding**: host VMs are deploy targets,
never edit-in-place dev environments. An emergency hand-edit on a host gets a
same-day PR that lands the same change at source — a host divergence that
outlives its incident is a bug.

## Pre-Merge Gate

**Canonical pre-merge check:** run
`python3 scripts/hooks/git_push_guard.py --check-pr <N> [--repo OWNER/REPO]`
BEFORE proposing a merge. It runs the SAME functions the enforcement gate uses
(mergeable → CI → base-invariant → Codex-freshness → scheduled-Claude-review →
review-body → inline findings), so the report and the gate can never disagree —
this `--check-pr` read IS the mandatory pre-merge step: **always run it and read
the PR's automated-review comments (Codex, leak/CI, the scheduled Claude review)
before any merge** — never hand-roll a
gh/jq review check (a hand-rolled query once used the GraphQL bot login on the
REST endpoint, matched nothing, and reported "Codex clean" while P2s sat unread).
When all gates pass it prints the exact atomic merge command to copy
(`... --match-head-commit <verified-head>`); use that command verbatim.

`git_push_guard.py` enforces a **hard gate** at merge time. Beyond the review
findings below, a gated `gh pr merge`:
- must carry `--admin` (explicit approval flag) and be bound to the reviewed head
  via `--match-head-commit` (GitHub rejects it server-side if the head moved —
  TOCTOU defense); the `--check-pr` command supplies this;
- requires Codex to have reviewed the **current head** — Codex does NOT auto-review
  a later fix-commit, so comment `@codex review` and wait after any push.
  **Clean-comment freshness:** a *clean* Codex re-review is posted as an ISSUE
  COMMENT ("Codex Review: Didn't find any major issues. … **Reviewed commit:**
  `<sha>`"), not a review object, so the reviews API never sees it. The gate now
  ALSO accepts that clean comment when its `Reviewed commit` sha names the current
  head — so a genuinely-clean re-review no longer false-blocks (it used to force a
  `# stale-review-override`). Fail-closed: the clean marker alone never vouches; a
  parseable `Reviewed commit` sha at head is required, and the comment must be
  authored by the Codex bot.
  **Smart-delta narrowing:** a STALE review passes anyway when the unreviewed
  delta (`reviewed...head` via the compare API, classified by `review_scope`
  substantiality) is provably review-trivial (docs-only / a small single-file
  touch-up) — the merge is then still bound to the exact head that was
  classified. A substantial or unclassifiable delta blocks; an ABSENT review
  always blocks;
- requires **every scheduled Claude review at the current head**. Each scheduled
  Claude review (a `/schedule` cloud routine) posts as the repo OWNER's account and
  must carry a marker `<!-- genesis-scheduled-review: head=<full-40-hex-sha> kind=<name> -->`
  naming the exact head it reviewed AND which routine it is (`kind`). The gate blocks
  unless an owner-authored marker for EVERY effective required kind
  (`_required_scheduled_review_kinds()` — DEFAULT `code-review` + `leaks`; the leak/secret
  scanner is irreducible and always required; an install may relax the OPTIONAL kinds to
  ADVISORY via `merge_gate.required_scheduled_reviews: [<kinds>]` in local `genesis.yaml`)
  names the PR's current head — so if any required routine never ran, ran on a stale
  commit, or was rate-limited, the merge blocks (naming the missing kinds). An ADVISORY
  routine still posts its review on the PR to be read/addressed, but its absence does not
  block. The block message is an **inventory**, not a diagnosis: under each missing
  kind it lists EVERY marker block the scan found that names that kind, with its
  status, and hides nothing. Run `python3 scripts/hooks/git_push_guard.py --check-pr <N>`
  — it renders those
  rows, not just the summary line (whose `present: none` clause reads like "nothing was
  posted" in every case below, and is the exact wording an operator was once measured
  acting wrongly on). Row statuses you will see:
  * *accepted at a DIFFERENT head* — a routine ran, then a push moved the head. Routines
  are generally not re-run on a push; re-review the current head and post the marker;
  * *REFUSED* (at this head or another) — the body reads as carrying a blocking finding
  and no clean-verdict line overrides it; see the clean-verdict rule below. The message
  deliberately does NOT print the verdict string, because a gate that prints the line
  that makes it pass is explaining how to get past itself;
  * *could not be counted: <reason>* — a head that is not full 40-lowercase-hex, an
  empty or refused field value (quoted back verbatim), an author who is not the repo
  owner, a dismissed review, a stale unpublished draft;
  * *unscoped* — blocks naming no REQUIRED kind, listed and credited to nothing:
  guessing which review a block "meant" would steer you into attesting for one that
  never ran. A value carrying a `/status` suffix (`kind=leaks/failed`) lands here and
  is flagged as a run that reported its own failure.
  The one conditional is a COUNT, keyed on a fact: a kind with no OWNER-authored block at
  any head gets "a routine may still be in flight — waiting is the right move", because
  that is the only state where patience can help. A stranger's comment is not evidence
  about the owner's routine and never silences that note; an owner's block in ANY state
  (accepted elsewhere, refused, dismissed, stale draft, malformed) is, and does.
  Why an inventory and not a diagnosis: the previous shape picked one cause per kind and
  hid the rest, and every one of nine review findings across six rounds was a hidden fact
  — a refused `[P1]` on an older commit hidden behind a typo at the current one, the only
  evidence a review had ever run hidden by a drive-by comment. Precedence is the right
  shape for a VERDICT; for a REPORT, hiding a true fact is never correct.
  Or append `# scheduled-review-override` to merge anyway (the conscious "merge without
  the scheduled reviews" case). Head match is EXACT for the LLM marker — no delta
  tolerance, unlike the Codex freshness gate, which grants relief on a provably trivial
  delta. That asymmetry is deliberate: the Codex classifier judges code-review
  substantiality by file type and size, and an inferential leak lands in exactly the small
  doc edit it would wave through. The ONE relief the leaks kind gets is MECHANICAL, not a
  delta tolerance: an ACCEPTED `leaks` marker on an ANCESTOR commit of head satisfies the
  gate when the `leak-detector` job of the `CI` workflow is green at head (identity pinned
  to that (name, workflow) pair; `_MECHANICAL_RESCAN_BY_KIND`), and NEVER when any
  refused `leaks` marker — or any blocking finding the scan could not credit to a head
  or kind (a tie, a malformed marker) — exists anywhere in the PR (the head axis is not
  a time axis — a later acceptance at an older head must not outrank a refusal). `--check-pr` renders a
  carried marker as `ok (leaks carried from <sha>, leak-detector green at head)`, never
  as `ok (at head)`. Measured motive: 6 of 10 sampled multi-push PRs were blocked purely
  because the routine does not re-stamp after a push, and the override had become
  routine. The marker means "ran **clean**", not merely "ran": a
  review whose body carries a blocking finding (`[P1]`/`HARD BLOCK`/`### ERROR`, unless a
  clean verdict overrides) is rejected, and DISMISSED/PENDING(draft) reviews don't count.
  **ALWAYS end a genuinely-clean scheduled review with an explicit verdict line**
  (`VERDICT: PASS`, or `PII/Secrets/Wording: CLEAN`). The blocking patterns are plain
  substrings with no negation awareness, so prose like "not a hard block" or "no hard
  blocks found" TRIPS them — measured on two 2026-08-28 PRs whose markers both contained
  that phrase in negated prose; the one that also carried a clean-verdict line was
  accepted and the one without it was silently refused. Without the verdict line a clean
  review can be rejected on wording alone;
  Fail-closed: an unreadable comments/reviews fetch BLOCKS (never a false all-clear);
- requires the PR base to equal the repo's default branch (retarget guard);
- blocks unless mergeability is a definite `MERGEABLE` (a failed/unknown read
  does not merge).
- **the CI gate** blocks red/pending checks; and — on the canonical public repo,
  where CI always runs — an `absent` CI state (a readable EMPTY check set = CI
  never ran, the tell of a conflicting branch or a dropped `pull_request` trigger)
  also blocks, so an un-CI'd PR can't merge. Likewise `incomplete` (a NON-empty
  rollup whose present checks are green but a REQUIRED workflow contributed no
  verdict — e.g. a lone green CodeQL after a workflow-specific trigger drop, or a
  fully-SKIPPED suite): the required identity is the rollup `workflowName`, config
  driven via `merge_gate.required_ci_workflows: [<names>]` in local `genesis.yaml`
  (default `CI`; fail-closed to the default on any malformed/empty config — there
  is no disable value). An UNREADABLE CI read (`unknown`) fails OPEN, and off the
  canonical repo `absent`/`incomplete` fail open too (another repo may
  legitimately have no CI, or a differently-named suite). Waive with
  `# ci-override` (never `--admin`).
- **Override sigils are split by boundary** so one waiver can't silently disarm
  an unrelated gate: `# review-override` waives ONLY the finding scans
  (review-body + inline P1s); `# stale-review-override` waives ONLY the
  review-context gates (Codex-at-head freshness + base-invariant);
  `# scheduled-review-override` waives ONLY the scheduled-Claude-review gate; CI is
  `# ci-override`. Append several sigils in one trailing comment when several
  waivers are genuinely intended — but the right fix for a stale review is
  `@codex review`, not the sigil.

The review-findings gate specifically:

1. After CI passes, the merge hook automatically checks PR comments
   for automated review findings (ERROR, [P1], HARD BLOCK).
2. If review present with **blocking findings** → merge is **BLOCKED**
   by the hook (exit code 2). Fix the findings first.
3. Inline findings are SCORED — P1 = 1.0, P2 = 0.5 — and the gate blocks at
   score >= 1.0 (any P1, OR >= 2 P2s). A lone P2 is advisory (0.5, allowed); a
   P2 is excluded from the score if a MAINTAINER reply engages it or it is on a
   documentation path. Pure WARNINGs/NOTEs (non-P1/P2) → merge allowed.
4. If no review comments at all (quota exhausted) → merge allowed
   on CI alone. Note in PR that review was quota-limited.
5. **Override**: Append `# review-override` to the merge command to
   bypass the gate (e.g., `gh pr merge 123 --squash --admin  # review-override`).
   The override is logged. Use only when findings are intentionally accepted.
6. **Read the PR's warning comments before merging — not just the hard gate.**
   Beyond Codex, a structural-review bot posts under the repo-owner account
   (review state COMMENTED) and emits **SOFT WARNINGs** (PII /
   private-text / wording) that the hook does NOT block on and that a naive
   `.comments` scan misses. Check BOTH `gh pr view N --json reviews,comments`
   and `gh api repos/<owner>/<repo>/pulls/N/comments`, and address each soft
   warning or consciously accept it. Never merge past an unread warning.
7. **Codex findings are INLINE review comments — invisible to `gh pr view`.**
   Codex's review *body* is boilerplate ("Here are some automated review
   suggestions"); its actual `[P1]`/`[P2]` findings live only at
   `gh api repos/<slug>/pulls/N/comments`. Derive `<slug>` live —
   `gh repo view --json nameWithOwner --jq .nameWithOwner` — NEVER hardcode
   it (configs name several repos; the working repo is not the org default).
   A **404 from that endpoint means WRONG SLUG or PR number, never "no
   findings"** — a clean PR returns `[]`. The merge-gate hook blocks on the
   weighted inline SCORE (P1=1.0, P2=0.5; block at >= 1.0), so a lone P2 is
   advisory but TWO unresolved P2s block — unread P2s no longer slip through in
   pairs (2026-07-10: 8 real P2s on the entity-layer PRs merged past the OLD
   P1-only gate, the exact gap this score closes). And the two
   channels are INDEPENDENT: Codex can post a quota/usage-limit message as an
   ISSUE comment while a later `@codex review` trigger delivers real inline
   findings anyway — a quota message is evidence about that channel at that
   moment, never proof Codex "can't review". After time passes, re-trigger and
   check the INLINE endpoint before concluding quota-limited (2026-08-26: #1484's
   real P2 arrived inline while the issue-comment channel still showed only the
   earlier quota message).
8. **A CONFLICTING PR silently suppresses the whole CI suite.** When a PR
   has a merge conflict with main, GitHub cannot build the merge ref, so
   `pull_request`-triggered workflows (the entire ci.yml suite) never run —
   while CodeQL still passes on the head SHA, making the check list LOOK
   green. A thin check list (only Analyze/CodeQL) means CHECK
   `gh pr view N --json mergeable` — `CONFLICTING` needs the base branch merged
   in before any CI verdict exists at all (2026-07-16: #1089 sat
   conflict-suppressed through three pushes; main had moved under it via
   concurrent sessions). Since #1484 the merge gate ENFORCES this class
   mechanically on the canonical repo: a fully-empty rollup reads `ci: absent`
   and blocks, and a thin/partial rollup missing the required CI workflow reads
   `ci: incomplete` and blocks (see the CI-gate bullet above) — the trap text
   stays because the DIAGNOSIS (check `mergeable` first) is still the fastest
   route to the cause.

## Reference Router

Read references ONLY when relevant to the specific task. Do NOT load all
references on every trigger.

| When you need... | Read... |
|---|---|
| Subsystem purpose/maturity/do-not-touch (judgment layer) | `docs/architecture/CURRENT.md` |
| Codebase structure, package map, gotchas, debugging | `references/codebase-map.md` |
| Package/module/symbol navigation (progressive drill) | `codebase_navigate` MCP tool (L0→L1→L2) |
| venv, DB paths, Qdrant, Ollama, network, commands | `references/environment.md` |
| Worktree rules, concurrent sessions, branch naming | `references/worktrees.md` |
| tracked_task, exc_info, os.killpg, logging patterns | `references/observability.md` |
| V3 state, build order, GROUNDWORK, architecture docs | `references/architecture.md` |
| Phase 6 contribution pipeline, sanitizer | `references/contribution.md` |
| Pending work, active incidents, subsystem status | `references/build-state.md` |
| Auditing/deep-reviewing AI-generated code (failure taxonomy, audit passes) | `references/ai-code-audit.md` |
| Which code tool to use (CBM vs Serena vs GitNexus vs Grep) | `.claude/docs/code-intelligence.md` |

**Freshness rule:** On first read of `codebase-map.md` in a session,
verify structural claims against current code. If a package status or
gotcha has changed, flag to user before acting on stale assumptions.
`docs/architecture/CURRENT.md` carries per-entry `verified:` stamps
enforced by `scripts/check_subsystem_map.py` (CI `subsystem-map-check`) —
after changing a subsystem's capabilities, update its entry and stamp.

## Public Repo & Release Workflow

The public repo (`GENesis-AGI`) is the primary development repo.
Standard open-source workflow: PRs go directly to the public repo.

- **Squash merges only** — merge commits are disabled on the public repo.
  Always `git pull --rebase origin main` after merging a PR before
  committing locally, or push will be rejected (non-fast-forward).
- **README is public-authoritative** — the public repo's `README.md` is
  hand-crafted and must NEVER be overwritten.
- **CHANGELOG audience is users** — only include entries a user updating
  their install would care about. No internal refactors, README changes,
  CI tweaks, or process artifacts. Lead with the user-visible effect, not
  the implementation technique.
- **No sensitive data in commits** — voice data, research profiles, IPs,
  and secrets must never enter the repo. User data lives in overlays
  outside the repo (e.g., `~/.claude/skills/*/`, `~/.genesis/`).
- **Individual campaigns are user data, not infrastructure** — a campaign's
  name/prompt/targets/cadence live only in the `campaigns` DB table and the
  private backups repo; never hardcode them into tracked source. Unlike modules
  (which ship defaults under `config/modules/*.yaml`), campaigns ship ZERO
  defaults (no `config/campaigns/`). Only campaign infrastructure ships. Express
  reusable session types as generic roles (e.g. the `community-responder`
  profile), not names coupled to a live campaign. See `src/genesis/campaigns/__init__.py`.
- **External egress is gated; owner-facing egress is not** — any autonomous send to the
  outside world (Discord, Medium, Twitter/X, Slack, `DistributionManager.distribute`) MUST
  route through the capability shadow-gate (`autonomy/shadow_gate`) before the enforce stage;
  the `scripts/check_external_io.py` CI guard backstops new endpoints. Delivery TO the owner
  (Telegram/voice/email-to-owner) is NEVER gated. Full contract in `autonomy/shadow_gate.py`.
