# Concurrent Sessions & Worktrees

> Expanded reference with examples and edge cases: `.claude/docs/concurrent-sessions.md`

Multiple Claude Code sessions may work on this repo simultaneously. Rules:

- **MANDATORY: Use git worktrees** for isolation when ANY other session might
  be active. Each session works in its own worktree off `main` via
  `.claude/worktrees/`. Never commit directly to `main` from a worktree.
- **NEVER commit directly to `main` when another session is active.** Pre-commit
  hook warns on direct-to-main commits.
- **NEVER use `git add .` or `git add -A`.** Always stage specific files by
  name. Broad staging is how one session's changes bleed into another's commit.
- **Branch naming**: `<scope>/<description>` (e.g., `agent/awareness-loop`).
- **NEVER run `pip install -e` pointing to a worktree.** The editable install
  is system-wide — it redirects ALL processes (bridge, watchdog) to load
  code from the worktree instead of main. This caused an I/O death spiral and
  repeated system crashes on 2026-03-16. Use `PYTHONPATH` instead.
  Enforced by PreToolUse hook.
- **NEVER assume other worktrees are stale.** Always treat them as active
  sessions with uncommitted work. When the pre-commit hook blocks a main
  commit due to worktrees: USE A BRANCH. Never try to remove worktrees to
  bypass the hook. Never `git worktree remove` without explicit user
  confirmation. The correct response is always: create a branch, commit
  there, merge later.
- **Before committing, always run `git diff --cached --stat`** and verify every
  file in the diff belongs to your work. If you see files you didn't modify,
  STOP and investigate.

## Merge Hook Gap (Known)

There is a pending infrastructure item to add a PreToolUse hook enforcing
`git merge`/`git push` separation. Currently not implemented — relies on
manual discipline and the `feedback_merge_push_chain.md` memory rule: each
step (commit/merge/push) needs separate user confirmation.
