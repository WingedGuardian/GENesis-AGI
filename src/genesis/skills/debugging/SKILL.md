---
name: debugging
description: Systematic debugging of issues — use when a test fails, runtime error occurs, unexpected behavior is reported, or an awareness tick produces anomalous results
consumer: cc_background_task
phase: 6
skill_type: workflow
---

# Debugging

## Purpose

Systematically diagnose and resolve bugs, failures, or unexpected behavior
in Genesis or its dependencies.

## When to Use

- A test fails unexpectedly.
- Runtime error or unexpected behavior is reported.
- An awareness tick or reflection produces anomalous results.
- Obstacle resolution escalates a technical issue.

## Workflow

1. **Reproduce** — Confirm the issue. Get the exact error, stack trace, or
   unexpected output. Define "expected vs. actual."
2. **Isolate** — Narrow the scope. Which module? Which function? Which input
   triggers it? Use binary search on the call chain.
3. **Hypothesize** — Form 2-3 candidate explanations. Rank by likelihood.
4. **Test hypotheses** — Write a minimal test or add logging to confirm/deny
   each hypothesis. Start with the most likely.
5. **Fix** — Apply the minimal correct fix. Do not fix adjacent issues in
   the same change.
6. **Verify** — Run the failing test. Run the full test suite. Confirm no
   regressions.
7. **Document** — Record the root cause and fix as an observation. Update
   procedures if the bug class is recurring.

## Output Format

```yaml
issue: <one-line description>
date: <YYYY-MM-DD>
root_cause: <what actually went wrong>
fix: <what was changed>
files_modified:
  - <file path>
regression_risk: low | medium | high
lesson: <what to remember to prevent recurrence>
```

## Examples

### Example: Hook fails in non-login shell

**Trigger:** Post-commit hook throws `KeyError: 'HOME'` in CI-like environment.

**Expected output:**

```yaml
issue: Post-commit hook crashes when HOME not set in non-login sessions
date: 2026-04-06
root_cause: Hook script reads os.environ["HOME"] but non-login shells
  (systemd, cron) strip HOME from environment
fix: Guard with os.environ.get("HOME", "/root") fallback + persist HOME
  in /etc/environment during install
files_modified:
  - .claude/hooks/genesis-hook
  - scripts/install_guardian.sh
regression_risk: low
lesson: Never assume HOME exists — non-login shells strip it. Always
  use get() with fallback for environment variables in hook scripts.
```

## References

- `tests/` — Test suite for verification
- `src/genesis/learning/procedural/` — Procedure updates for recurring patterns
