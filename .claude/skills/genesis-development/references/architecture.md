# Genesis Architecture & Build State

## Current State

Genesis core is feature-complete. V4 work is active (meta-prompting,
adaptive weights, channel learning, L5+ autonomy).

**Ego sessions** (`src/genesis/ego/`) are **ACTIVE** (v3.0a11, PRs #26/#27).
Two egos: user ego (CEO, Opus) and Genesis ego (COO, Sonnet). Registered
in bootstrap, running on adaptive cadence via the awareness loop.

## Groundwork Code Protection

- Tag with: `# GROUNDWORK(<feature-id>): <why this exists>`
- **NEVER delete or refactor GROUNDWORK-tagged code as "dead code"**
- Only remove when the feature is fully active or the user explicitly
  cancels it

## Key Architecture Documents

1. `docs/architecture/genesis-v3-vision.md` — Core philosophy and identity
2. `docs/architecture/genesis-v3-autonomous-behavior-design.md` — Primary
   design
3. `docs/architecture/genesis-v3-build-phases.md` — Safety-ordered build
   plan
4. `docs/architecture/genesis-v3-dual-engine-plan.md` — Multi-engine
   strategy
5. `docs/architecture/genesis-v3-gap-assessment.md` — Pre-implementation
   risks

## Design Principles (Genesis-Specific)

These supplement the general principles kept in CLAUDE.md:

- **File size discipline** — Target ~600 LOC per file, hard cap 1000 LOC.
  When a file grows past 600, plan a split. When it hits 1000, split before
  adding more. Use the package-with-submodules pattern: convert
  `big_module.py` into `big_module/__init__.py` + focused submodules,
  re-exporting from `__init__.py` for backward compatibility. Keep a shim
  at the old import path if external code depends on it. `runtime.py` is
  the canonical example — now `runtime/` with 20 init modules.
- **Tool scoping: don't handicap autonomous sessions** — When dispatching CC
  sessions with `skip_permissions=True`, `allowed_tools` (whitelist) is
  ignored — `--dangerously-skip-permissions` overrides it (empirically
  verified 2026-03-17). Use `disallowed_tools` (blacklist) to exclude
  specific dangerous tools; blacklists ARE respected with skip-permissions.
  Use PreToolUse hooks in `.claude/settings.json` for granular tool-level
  guards — hooks fire in ALL sessions including `claude -p`.
- **`$CLAUDE_PROJECT_DIR` is command-string only.** Claude Code resolves
  `${CLAUDE_PROJECT_DIR}` in hook commands in `settings.json`, but does NOT
  export it as a shell environment variable. Hook scripts must NOT read
  `os.environ["CLAUDE_PROJECT_DIR"]` — it will be empty. Use the
  `.claude/hooks/genesis-hook` launcher, which self-locates from its
  filesystem position.
- **A wrapper that sanitizes its OWN input has not sanitized its CHILD's.**
  The launcher scrubbed git's location variables (`GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_INDEX_FILE`, …) for its own `git rev-parse` discovery long before it
  scrubbed them for the hook it `exec`s — so it protected WHICH script runs and
  not what that script's own git queries see. A launched hook inherited the
  ambient overrides and resolved a foreign repository despite being handed an
  explicit `cwd`. MEASURED 2026-09-17: all four decision inputs the enforcement
  hooks share moved, every one in the FAIL-OPEN direction — branch
  `main` → the other repo's branch, staged-diff hash → the `"clean"`
  nothing-staged sentinel, worktree marker key → a marker that cannot exist, and
  substantiality `substantial` → `inline`. Two layers close it and both are
  needed: the launcher scrubs for the child (covers every launched hook,
  including the ones that scrub nothing themselves), and `review_state` /
  `review_scope` scrub at their own git runners (covers direct invocation from a
  shell, which the launcher never sees — `git_push_guard.py --check-pr` is run by
  hand constantly). The variable list is duplicated in all three places because
  bash cannot import a Python tuple; `tests/test_hooks/test_git_env_scrub.py`
  pins the copies to each other rather than trusting them. **The general rule:
  when a process hands work to a child, decide explicitly what the child's
  environment is — inheriting it is a decision too, just an unmade one.**
