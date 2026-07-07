# Concurrent Sessions & Worktrees

> Expanded reference with examples and edge cases: `.claude/docs/concurrent-sessions.md`

Multiple Claude Code sessions may work on this repo simultaneously. Rules:

- **MANDATORY: Use git worktrees** for isolation when ANY other session might
  be active. Each session works in its own worktree off `main` via
  `.claude/worktrees/`. Never commit directly to `main` from a worktree.
- **Create worktrees with `git worktree add` — NOT the `EnterWorktree` tool.**
  `EnterWorktree` *relocates the live session* into the worktree: the harness
  re-roots the transcript under a separate `…--claude-worktrees-<name>` project
  slug and leaves only a `wt-<id>.jsonl` stub behind, so the conversation
  disappears from `/resume` in the main repo (it looks "lost"). A PreToolUse
  hook (`worktree_cwd_guard.py --enter-worktree`) hard-blocks it. To isolate
  work while staying findable: `git worktree add .claude/worktrees/<name> -b
  <scope>/<desc> origin/main`, then edit via the worktree's ABSOLUTE paths and
  run tests with `PYTHONPATH=<worktree>/src pytest <files>` — your session stays
  in the main repo and in `/resume`. For parallel isolated work, dispatch a
  subagent (Agent tool, `isolation="worktree"`). If a worktree-ROOTED session is
  genuinely wanted, the USER launches Claude Code from that directory.
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
- **NEVER boot the full Genesis runtime (`genesis serve`) from/against a
  worktree** — not even with the systemd unit stopped first. Spawned children
  inherit the worktree `PYTHONPATH`, and path-keyed subsystems (Serena LSP,
  code indexers, GitNexus) treat the worktree as a NEW ~190K-LOC project and
  cold-index it in parallel. This OOM-crashed the container on 2026-07-03.
  `PYTHONPATH` to a worktree is for **pytest only**. To verify worktree code
  at runtime: merge-then-verify with instant `git revert` (for isolated /
  frontend-only diffs), or a minimal Flask harness registering only the
  blueprint under test. Enforced by PreToolUse hook.
- **NEVER assume other worktrees are stale.** Always treat them as active
  sessions with uncommitted work. When the pre-commit hook blocks a main
  commit due to worktrees: USE A BRANCH. Never try to remove worktrees to
  bypass the hook. Never `git worktree remove` without explicit user
  confirmation. The correct response is always: create a branch, commit
  there, merge later.
- **Before committing, always run `git diff --cached --stat`** and verify every
  file in the diff belongs to your work. If you see files you didn't modify,
  STOP and investigate.

## Testing code in a worktree

`tests/conftest.py` pins `sys.path[0]` to the worktree's own `src`, so `pytest`
run from a worktree always tests THAT worktree's code — you don't need
`PYTHONPATH`, and setting it will NOT redirect pytest (the guard shadows the
editable install and any env path). Consequence: to prove a new regression test
actually fails on the unfixed code, you can't point pytest at old code via the
environment — revert the source in place instead:

```bash
git stash push -- path/to/file.py        # restore the unfixed code
pytest tests/.../test_x.py -k new_test    # expect RED
git stash pop                             # restore the fix
```

A standalone `python -c "import genesis; print(genesis.__file__)"` DOES honor
`PYTHONPATH`/the editable install, so it can misleadingly show main's path while
pytest is using the worktree's. Trust the stash check, not the env var.

## Shell discipline across checkouts (cwd drift)

The conftest guard only helps when pytest runs FROM the worktree. The Bash
tool's working directory persists across calls, and any command chain
containing `cd` — even a side-errand like `cd ~/genesis && git fetch` —
silently re-roots EVERY later relative path. In a multi-checkout session this
mutates the WRONG tree: on 2026-07-06 a `cat >> tests/…` appended new tests to
MAIN's tree and a bare `pytest tests/…` then ran them against main's src —
false RED/GREEN signals (the tests exercised code without the feature under
test) plus a stray uncommitted edit in main for concurrent sessions to trip
over.

- **Mutating commands use ABSOLUTE paths, always**: `cat >>`, `sed -i`, `cp`,
  `mv`, and `git` staging/committing (or `git -C <worktree> …`).
- **Test runs are self-rooting**: `cd <worktree> && pytest …` as ONE compound
  command, every time — never a bare `pytest` relying on remembered cwd.
- After running any command chain that contains a `cd`, treat the cwd as
  unknown until re-established.
- Diagnostic tell: a should-be-RED test "passes for the wrong reason," or a
  feature test fails on the feature's own symbols being missing — check `pwd`
  and the imported `module.__file__` before debugging the code itself.
- Recovery: `git status` the polluted tree; if the diff is exactly the stray
  edit, `git checkout -- <file>` and redo via absolute paths.

## Push/Merge Enforcement

`git_push_guard.py` (PreToolUse hook) blocks:
- `git push` to main/master (any variation — bare, with remote, with refspec)
- `git merge` when on main/master

All code changes must go through PRs. The only merge path is
`gh pr merge --squash --admin` after explicit user approval. Each step
(commit → push branch → create PR → merge) needs separate user confirmation.
