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

## 1. Scope the FULL branch diff (not just the last commit)

- `git diff "$(git merge-base HEAD origin/main)"` — everything the branch changes vs the
  merge-base, INCLUDING staged AND unstaged work (`git diff <commit>` compares the working
  tree, so staged content is included). Add `--stat` for the file list. Do NOT use `..HEAD`
  alone (misses uncommitted work) or plain `git diff` alone (misses STAGED work) — the state
  you run this in is usually staged-but-uncommitted.

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

## 3. Triage → stage the class-level fix (don't commit yet)

3-bucket disposition: FIX real+material now / evidence-tiered documented-accept / escalate.
When you fix, fix the mechanism/class — a per-instance patch is what spawns the next review
round. `git add` the fix, but do NOT commit yet — the commit is the last step, after the marker.
If you're on your 3rd defect-bearing round, STOP at the escalation cap (see SKILL.md).

## 4. Record evidence, mark, THEN commit (satisfies the commit review-depth gate)

- `EVID="$(python3 scripts/review_state.py evidence-path)"` — the per-worktree evidence file.
- Write the reviewer's structured findings to `$EVID` — must carry a severity-ladder label
  (BLOCKER / SHOULD-FIX or CRITICAL/HIGH/MEDIUM/LOW/P1-P3), at least one `file:line`, and ≥400
  chars, or the gate will not accept it as adversarial.
- `python3 scripts/review_state.py mark --agent-output "$EVID"` — add `--clean` IFF the pass
  found NO new BLOCKER/SHOULD-FIX/P1/P2 (resets the escalation streak; a forgotten `--clean` is
  the safe direction).
- Run `mark` AFTER the final `git add` and BEFORE `git commit`: the marker binds the *staged*
  content, so don't restage after marking, and mark + commit within 30 min of writing the
  evidence (it expires).

For a quick self-check of a small change, use `/audit-changes` instead — this command is the
heavyweight adversarial pass.
