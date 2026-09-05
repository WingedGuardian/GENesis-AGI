# Concurrent Sessions & Worktrees

> Expanded reference with examples and edge cases: `.claude/docs/concurrent-sessions.md`
> — including **Coordinating With a Peer Session**: when to message another
> session, why a reply needs a higher bar than a send, and why a peer's claim
> is a lead rather than a fact.

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

`git_push_guard.py` (PreToolUse hook) gates:
- `git push` (any branch) — the one action that publishes code externally. An
  **interactive** session gets a native approve/deny dialog
  (`permissionDecision:ask`) that only *you* can satisfy; a Genesis-**dispatched**
  session (`GENESIS_CC_SESSION=1`) is **hard-denied** (no human to prompt; real
  autonomous delivery goes through the scope-gated server path, not the CC Bash
  tool). Force push (`--force` / `--force-with-lease` / `-f` / `+refspec` /
  `--mirror`) is **hard-blocked** in every session. Multiple pushes in one
  command are blocked — each push needs its own approval.
- `gh pr create` — a create only publishes code in the **implicit** form (no
  `--head`), where gh may *push* (and can fork) the current branch when it isn't
  fully on the remote. So an **explicit `--head`** (local, unpushed, or
  `owner:fork`) is **un-gated** — `gh pr create --help`: "Use `--head` to
  explicitly skip any forking or pushing behavior." The implicit form is un-gated
  **only when the current branch is already on the remote** (then it's just a
  review request on pushed code); if it isn't, it's gated like a push (interactive
  → ask, dispatched → deny). The "already pushed?" check hits the **actual remote**
  via `git ls-remote` (not the local tracking ref, which goes stale when a merged
  branch is deleted), fail-safe — any network error / timeout / uncertainty gates.
  On the un-gated path the hook emits an explicit **`allow`** so Claude Code's own
  permission prompt doesn't re-fire for the (non-allow-listed) command — so a
  `git push && gh pr create` prompts once (for the push) and the create rides
  along, and a standalone create on pushed code doesn't prompt at all.
- `git merge` into main/master — **hard-blocked** (use the PR workflow).
- `gh pr merge` without `--admin`, or with unresolved review findings —
  **hard-blocked** (a `# review-override` trailing comment acknowledges
  intentionally-accepted findings on the *merge* command only).
- `gh pr close` (and its REST/GraphQL equivalents — a `gh api … PATCH` to the
  PR's `/pulls/N` **or** `/issues/N` resource with `state=closed`, or a
  `gh api graphql … closePullRequest` mutation) — closing a PR abandons reviewed
  work and is irreversible-outward,
  so it is a **user decision**, never the session's. An **interactive** session
  gets a native approve/deny dialog it cannot self-satisfy; a **dispatched**
  session is **hard-denied** and pointed at `outreach_send_and_wait` to ask the
  user. Closing a PR that reached the review **terminal** (`FINAL_ROUND_CAP`
  rounds, counted from the PR's real review history) additionally requires a
  written rebuild-commitment first — closing work that could not pass review is
  "back to the drawing board", not the end — and its opening is quoted into the
  approval dialog. Directed by the user on 2026-09-04 after PR #1579 was closed
  without their approval. `gh issue close`, `gh pr comment`, `gh pr edit`, and
  `gh pr reopen` are **not** gated — the point is to stop a session *deciding*
  to abandon reviewed work, not to stop it speaking or labelling.

The push dialog replaced the old `# review-override` token for the push gate —
the agent can no longer self-approve a push. All code changes go through PRs;
the only merge path is `gh pr merge --squash --admin` after explicit user
approval.
