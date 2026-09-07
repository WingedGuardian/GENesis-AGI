Run a fresh-context, un-anchored ADVERSARIAL-LOGIC review of the current diff before
pushing — the review that catches real bugs, not a lint/secrets/"structural CLEAN" scan.
Use it on any substantial change, and ALWAYS before pushing a hook, gate, parser, or
security-surface change.

Why this exists: an internal review-loop analysis of recent PRs found that first-round Codex
findings were real, pre-existing bugs — the initial pushes had been self-reviewed with the
WRONG SHAPE (a pattern/secrets scan), not an adversarial logic audit. This command makes the
right-shape pass the easy default. (It reliably kills the first round; it is NOT a cure for the
whole loop — fix-churn and hand-rolled parsing drive the tail. See the Common Traps entry on
CLI parsing.)

Note: `/deep-review` is the LOCAL pre-push adversarial pass (Claude-model reviewers). It does
NOT replace the independent-model Codex review, which still runs on the PR and is required by the
merge gate — the two are complementary (Codex catches cross-model blind spots). This command
clears the local commit review-depth gate; it does not certify the PR by itself.

## 1. Stage everything, then scope the FULL branch diff

- `ROOT="$(git rev-parse --show-toplevel)"` — the repo root. Use `$ROOT/scripts/…` for every
  `scripts/…` path below: this repo's Bash cwd drifts, so a bare relative `scripts/…` can
  resolve against the wrong directory and fail. (`git` commands find the root on their own.)
- **Stage all intended changes first** (`git add …`) and skim `git status`: `git diff` never
  shows UNTRACKED (never-added) files, so a brand-new source/test/hook/config file is invisible
  to the review until it is staged. Nothing untracked = the diff is complete.
- `git diff "$(git merge-base HEAD origin/main)"` — everything the branch changes vs the
  merge-base, INCLUDING staged and unstaged tracked work (`git diff <commit>` compares the
  working tree). Add `--stat` for the file list. Do NOT use `..HEAD` alone (misses uncommitted
  work) or plain `git diff` alone (misses STAGED work).

## 2. Dispatch the reviewer(s) — FRESH CONTEXT, sequential, un-anchored

Use the Agent tool. Do NOT self-review — a self-review is anchored to your own blind spots,
which is exactly what lets Round-1 bugs through.

- **`genesis-architect`** over the full diff — follow its protocol
  (`.claude/agents/genesis-architect.md`): scope-drift check first, BLOCKER / SHOULD-FIX / NOTE
  ladder with per-finding `file:line` + confidence, completion status last.
- **ALSO `genesis-security-reviewer`** when the diff touches auth, credentials/secrets,
  subprocess, SQL, path handling, external input (Telegram/dashboard/MCP), or hooks/gates.
- Run them SEQUENTIALLY, never in parallel (standing rule — the second reviewer must see the
  fixed code, and parallel doubles spend).

Prime each reviewer with the RIGHT SHAPE (what a lint scan misses). Paste this into the prompt:

> Assume there are bugs; enumerate the whole CLASS, not just the named cases. Read the
> authoritative source (the library/loader/protocol you mirror) END-TO-END before deriving.
> Apply the AI-code failure taxonomy in `.claude/skills/genesis-development/references/ai-code-audit.md`.
> Hunt specifically for: fail-OPEN where fail-closed was intended; unhandled states in a state
> machine; TOCTOU / non-atomic check-then-act; silently-swallowed errors that read as "all
> clear"; and **hand-rolled CLI / `gh` / bash / argv parsing** — flag that as an ARCHITECTURAL
> defect and recommend atomic binding + fail-closed-on-unparseable, NOT case-by-case patches
> (it is the root of the Codex review loop).
>
> ENUMERATE THE POPULATION BEFORE JUDGING IT. Name the call sites, consumers, states, branches
> or axes the change touches, THEN say which you examined. Findings are a sample drawn from
> that population; without it you cannot tell whether you sampled 3 of 3 or 3 of 9, and the
> next round's "new" defect is usually the member nobody listed.
>
> REPORT WHERE YOU FOUND NOTHING, as explicitly as what you found. A bare findings list cannot
> distinguish a CLEAN area from an UNEXAMINED one, so it silently reads as full coverage — and
> the reader cannot audit a gap they cannot see. "I looked hard at X and found nothing" is a
> first-class result; state it per area, not as a closing reassurance.
>
> Where a claim is load-bearing, RUN something rather than reasoning about it, and say what you
> ran and observed. Prefer a reading over an argument — including about the review's own
> instruments, whose output can be wrong in the flattering direction.

## 3. Triage → stage the class-level fix (don't commit yet)

3-bucket disposition: FIX real+material now / evidence-tiered documented-accept / escalate.
When you fix, fix the mechanism/class — a per-instance patch is what spawns the next review
round. `git add` the fix, but do NOT commit yet — the commit is the last step, after the marker.
If you're on your 3rd EXTERNAL cross-model defect-bearing round, STOP at the escalation cap
(see SKILL.md) — internal self/subagent audits like this one never count toward it.

**A fix is not done until you have enumerated the CLASS'S OTHER MEMBERS and said what you
found there.** The reviewer named the instance it happened to reach; the same defect usually
sits at the sibling call sites nobody looked at, so patching only the named one leaves them
for the next round and makes the loop look like bad luck rather than an unfinished fix. Write
the enumeration down — "this shape appears at A, B and C; A was reported, B and C are fixed
here / are not affected because …". A fix with no such list is a claim about one line.

Two failure modes to watch for while fixing, both of which end the round *worse* than it
started, and neither of which the findings list will tell you about:

- **The fix that becomes the next finding.** When a round's defect was introduced by the
  previous round's fix, stop patching and change the MECHANISM — the design is generating
  them. Count findings by FILE across rounds; a file that keeps reappearing is the signal.
- **Widening a check under cover of a regression fix.** Restoring what a change broke is not
  the same as hardening a pre-existing case, and bundling them hides a new over-block inside
  a "fix". Measure the pre-existing case's real rate before touching it, and say the number.

## 4. Record evidence, mark, THEN commit (satisfies the commit review-depth gate)

- The marker binds the **final staged content**, not the text the reviewer saw. So after
  applying §3's fixes: re-inspect the fixes (re-run the reviewer if they are non-trivial) and
  write the evidence to describe the FINAL staged state — the findings AND that the fixes were
  applied and verified. Do not let a marker attach a pre-fix review to post-fix code no one
  re-read.
- `EVID="$(python3 "$ROOT/scripts/review_state.py" evidence-path)"` — the per-worktree evidence
  file. Write the findings to `$EVID`: must carry a validator-recognized severity label
  (BLOCKER / SHOULD-FIX / CRITICAL / HIGH / MEDIUM / LOW / P1-P3 — a security-reviewer WARNING is
  NOT recognized by the validator, so render it as SHOULD-FIX in the text), at least one
  `file:line`, and ≥400 chars, or the gate rejects the evidence as non-adversarial.
- **Carry the COVERAGE into the evidence, not just the findings.** The evidence is what a later
  reader — a closing session, the next round, whoever inherits this branch — has instead of the
  review. A findings-only file tells them what was wrong and NOTHING about what was checked, so
  a gap and a clean bill look identical on disk. State the population §2 enumerated, which parts
  were examined, and where you looked and found nothing. A `Scope Check:` heading is the
  conventional place to put it — the validator recognises that phrase in place of a severity
  ladder, though the `file:line` and length requirements above still apply on their own. It
  costs one paragraph.
- `python3 "$ROOT/scripts/review_state.py" mark --agent-output "$EVID"` — this is an INTERNAL
  (same-model) audit, so a plain `mark` is correct: it satisfies the commit review-depth gate and
  NEVER counts toward the cross-model escalation streak, whatever it found. No outcome flag is
  needed. (EXTERNAL is judged by the reviewing MODEL, not the gateway: only a non-Anthropic model —
  Codex or Kimi on .123 (NOT OpenRouter, which is not an approved method today; and never a Genesis
  internal model) — is marked `--source external --defects|--clean`; that alone moves the counter.)
- Run `mark` AFTER the final `git add` and BEFORE `git commit`. The evidence must be recent when
  you `mark` (its age is checked at mark time), so write-then-mark promptly. Once marked, an
  unchanged staged diff stays cleared by its diff-hash — if you restage or amend, re-mark.

For a quick self-check of a small change, use `/audit-changes` instead — this command is the
heavyweight adversarial pass.
