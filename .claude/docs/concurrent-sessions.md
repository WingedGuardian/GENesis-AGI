# Concurrent Sessions & Worktrees — Expanded Reference

This expands on the concurrent session rules in CLAUDE.md with examples,
edge cases, and the incidents that created each rule.

## Why Worktrees Are Mandatory

Multiple Claude Code sessions (foreground, background reflections, autonomous
tasks) can run simultaneously. Without isolation, one session's `git add .`
contaminates another's commit. This has happened — it led to commits containing
files from unrelated work, corrupted diffs, and hours of cleanup.

## Common Mistakes

### "I'll just commit directly to main — there are no other sessions"
You don't know that. Background sessions (reflections, inbox, surplus) run on
their own schedule. The pre-commit hook checks for worktrees and warns. If it
warns: USE A BRANCH. Never try to remove worktrees to bypass the hook.

### "I'll use `git add .` just this once"
Don't. Stage specific files by name. Always. `git add -A` is how one session's
changes bleed into another's commit.

### "This worktree looks stale, let me remove it"
NEVER assume worktrees are stale. They may have uncommitted work from a paused
session. Never `git worktree remove` without explicit user confirmation.

### "I'll run `pip install -e .` from the worktree"
NEVER. Editable installs are system-wide — this redirects ALL processes to load
code from the worktree. This caused an I/O death spiral and repeated system
crashes on 2026-03-16. Use `PYTHONPATH` instead.

## Pre-Commit Verification

Before every commit:
```bash
git diff --cached --stat
```
Verify EVERY file in the diff belongs to your work. If you see files you didn't
modify, STOP and investigate.

## Branch Naming

`<scope>/<description>` — e.g., `agent/awareness-loop`, `fix/reflection-telephone`.
