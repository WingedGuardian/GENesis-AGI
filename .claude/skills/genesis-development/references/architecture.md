# Genesis Architecture & Build State

## Current State

Genesis core is feature-complete. Preparing for V4 work (meta-prompting,
adaptive weights, channel learning, L5+ autonomy).

**Ego sessions** (`src/genesis/ego/`) are built but **inert until beta**.
Not registered in bootstrap; disabled by default.

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
