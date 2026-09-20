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
- **Normalizing an environment removes CAPABILITIES as well as influence — and
  `unset` is not the neutral option either.** Hardening a subprocess environment
  looks like it can only subtract an attacker's reach. It also subtracts the
  caller's, and both directions were MEASURED on one variable in one review round:
  * UNSETTING `GIT_CONFIG_GLOBAL` re-enables `$HOME/.gitconfig` (it is git's own
    documented way to say *read no global config*), so a scrub meant to isolate
    LOOSENS, and a caller that set it to `/dev/null` for isolation loses that.
  * PINNING it to an empty file removes `safe.directory`, which git reads ONLY
    from protected config, by design, so a repo cannot self-approve — and
    repo-local config cannot restore it. Under any uid mismatch (bind-mounted
    devcontainer, container CI, a hook under sudo) git then REFUSES with empty
    stdout. MEASURED end-to-end with a file staged: `get_current_diff_hash`
    returned `'clean'` — the NOTHING-STAGED sentinel — which is the exact
    fail-open the change was written to close. It also removes
    `credential.helper` and `url.*.insteadOf`, which the same env feeds to
    `git ls-remote` and `gh`.

  So the rule is: **follow the tool's non-zero-exit path all the way to the
  caller's sentinel before calling a hardening "strictly stronger".** An empty
  result and a refusal are the same bytes, and a gate that treats empty as
  "nothing to review" converts a refusal into a pass.
- **Prefer a FLAG on the command over surgery on the environment.** The route
  that actually moved a gate decision — a global `core.attributesFile` marking
  `*.py binary`, which collapses `--numstat` to `-  -` so the depth gate reads
  `inline` instead of `substantial` — is closed by
  `-c core.attributesFile=/dev/null` on the diff, exactly as `diff.external` is
  closed by `--no-ext-diff`. MEASURED: the flag closes the hole AND keeps
  `safe.directory` working (rc=0), where the environment pin closes the hole and
  breaks it (rc=129). The flag is also scoped to the one command that needs it,
  so it cannot disarm a sibling consumer the way a shared env builder does.
  `--text` is NOT a substitute (MEASURED: `--numstat` still reports `-  -`).
- **A guard that PREDICTS what a command will do must see what that command will
  see.** `git_push_guard._push_config_is_simple` is an allowlist over the user's
  effective config (`remote.pushDefault`, `push.default`, …) where a broadening
  value makes it PROMPT. MEASURED with a control that moves: with
  `push.default = matching` in `~/.gitconfig` it returns False (prompts) when the
  config is visible and True — allows silently — once `GIT_CONFIG_GLOBAL` is
  pinned away. **Same shape as "never normalize before a blind-spot probe", one
  layer up: there the normalizer and the probe sit in one script; here they sit
  in different processes, which is why it stayed invisible.** The launcher
  therefore scrubs the injection CHANNELS (`GIT_CONFIG_COUNT`,
  `GIT_CONFIG_PARAMETERS`), which have no legitimate use in a hook's
  environment, and never the config FILE variables, which name files the user
  owns.
