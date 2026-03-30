# Genesis v3 — Project Instructions

Genesis v3 is an autonomous AI agent system built on Agent Zero (`~/agent-zero`).

## Communication Style

Be gravity for this project. Attack weak points in reasoning, challenge
assumptions, expose what's missing. Don't sugarcoat. When there's a grounded
opportunity to improve an idea, take it — don't just agree.

**Cover the user's back.** Actively look for critical gaps in code changes,
architecture decisions, or technical direction. Challenge where needed, flag
what could go wrong, identify what's missing. The user expects you to catch
what they miss — that's part of your job.

## Environment

- **Python**: 3.12 (venv at `~/genesis/.venv`)
- **Node**: 20.x
- **Host VM**: Configured in `~/.genesis/guardian_remote.yaml` (set by
  `install_guardian.sh`). Guardian runs here. SSH access is Guardian-only
  via the `guardian-gateway.sh` command dispatcher. NOT the Ollama server.
  only. Do NOT install locally. NOT the host VM.
- **Qdrant**: `localhost:6333` (systemd service)
- **GitHub**: `WingedGuardian/Genesis`
- **Database**: `~/genesis/data/genesis.db` (NOT `~/genesis/genesis.db`)
- **Backups**: `YOUR_GITHUB_USER/genesis-backups`, cron every 6h (`scripts/backup.sh`)
- **Env scrub**: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is NOT used — Genesis
  hooks and MCP servers require inherited API keys (DeepInfra, Qwen, etc.).

## New Machine Setup

```bash
./scripts/bootstrap.sh                            # Full setup — venv, config, services, memory
```

## Common Commands

```bash
source ~/genesis/.venv/bin/activate               # Required for all Python work
cd ~/genesis && ruff check .                      # Lint all Python
cd ~/genesis && pytest -v                         # Run tests
cd ~/genesis && ruff check . && pytest -v         # Both (do before committing)
curl -s http://localhost:6333/collections | jq .  # Verify Qdrant
cd ~/agent-zero && python run_ui.py               # Agent Zero web UI (port 5000)
python scripts/setup_claude_config.py             # Regenerate CC config for this machine
```

## Design Principles

- **Flexibility > lock-in** — Adapter patterns, generic interfaces, pluggable
  components. Every external dependency should be swappable.
- **LLM-first solutions** — Code handles structure (timeouts, validation, event
  wiring); judgment calls belong to the LLM. Prefer better prompts over
  heuristics.
- **Quality over cost — always** — Cost tracking is observability, NEVER
  automatic control. No auto-throttling or auto-degrading. The user decides
  tradeoffs; Genesis provides the levers.
- **File size discipline** — Target ~600 LOC per file, hard cap 1000 LOC.
  When a file grows past 600, plan a split. When it hits 1000, split before
  adding more. Use the package-with-submodules pattern from the
  `split-large-files` refactor: convert `big_module.py` into
  `big_module/__init__.py` + focused submodules, re-exporting from
  `__init__.py` for backward compatibility. Keep a shim at the old import
  path if external code depends on it. `runtime.py` is the canonical
  example — now `runtime/` with 20 init modules.
- **Verify against actual code** — Docs describe intent; code describes reality.
- **CAPS markdown convention** — User-editable files that shape LLM behavior use
  UPPERCASE filenames (e.g., `SOUL.md`, `USER.md`). Transparency breeds trust.
- **Tool scoping: don't handicap autonomous sessions** — When dispatching CC
  sessions with `skip_permissions=True`, **`allowed_tools` (whitelist) is
  ignored** — `--dangerously-skip-permissions` overrides it (empirically
  verified 2026-03-17). Use `disallowed_tools` (blacklist) to exclude
  specific dangerous tools; blacklists ARE respected with skip-permissions.
  For broad autonomous sessions (reflection, task execution, strategic
  review, conversation), do NOT restrict tools. A system built for autonomy
  must not fight itself. The dispatching LLM cannot perfectly predict what
  the executing LLM will need. Err toward giving broad sessions full tool
  access. Use PreToolUse hooks in `.claude/settings.json` for granular
  tool-level guards (e.g., blocking WebFetch on YouTube URLs, blocking
  dangerous Bash commands) — hooks fire in ALL sessions including `claude -p`.
- **`$CLAUDE_PROJECT_DIR` is command-string only.** Claude Code resolves
  `${CLAUDE_PROJECT_DIR}` in hook commands in `settings.json`, but does NOT
  export it as a shell environment variable. Hook scripts must NOT read
  `os.environ["CLAUDE_PROJECT_DIR"]` — it will be empty. Use the
  `.claude/hooks/genesis-hook` launcher, which self-locates from its
  filesystem position.

## Concurrent Sessions & Worktrees

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
  is system-wide — it redirects ALL processes (bridge, AZ, watchdog) to load
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

## Dual-Repo Distribution

Two repos: private `GENesis` (working) and `GENesis-AGI` (public distribution).
Sync via `scripts/prepare-public-release.sh`. Full details: `.claude/docs/dual-repo.md`.

## Documentation

| What | Location |
|------|----------|
| Conventions & commands | `CLAUDE.md` (this file) |
| Architecture & design | `docs/architecture/` |
| Session-to-session learnings | `~/.claude/projects/.../memory/MEMORY.md` |
| Lessons learned, project rules | `docs/reference/` |

Session transcripts: `~/.claude/projects/{project-id}/*.jsonl` (project-id =
repo path with `/` replaced by `-`, derivable via `cc_project_dir()` from
`genesis.env`). Search with Grep/Read on demand. Background session transcripts
(reflections, inbox, surplus) are in the matching `--background-sessions/` dir.

## Key Architecture Documents

1. `docs/architecture/genesis-v3-vision.md` — Core philosophy and identity
2. `docs/architecture/genesis-v3-autonomous-behavior-design.md` — Primary design
3. `docs/architecture/genesis-v3-build-phases.md` — Safety-ordered build plan
4. `docs/architecture/genesis-v3-dual-engine-plan.md` — Multi-engine strategy
5. `docs/architecture/genesis-v3-gap-assessment.md` — Pre-implementation risks
6. `docs/architecture/genesis-agent-zero-integration.md` — AZ integration

## Agent Zero Integration

Genesis runs on Agent Zero (`~/agent-zero`). **CRITICAL**: `server_startup`
hook in `run_ui.py` must exist — without it, ALL background infra is dead.
Full integration details: `.claude/docs/agent-zero-integration.md`.

## V3 Scope Fence

V3 is conservative — no V4 meta-prompting, adaptive weights, channel learning,
procedural decay, or L5-L7 autonomy. No V5 identity evolution or meta-learning.
See design docs for full V4/V5 feature list.


## Groundwork Code Protection

- Tag with: `# GROUNDWORK(<feature-id>): <why this exists>`
- **NEVER delete or refactor GROUNDWORK-tagged code as "dead code"**
- Only remove when the feature is fully active or the user explicitly cancels it

## Observability Rules

> Expanded reference with code examples and anti-patterns: `.claude/docs/observability-rules.md`

- **Never use bare `asyncio.create_task()`.** Use `genesis.util.tasks.tracked_task()`.
- **Never use `contextlib.suppress(Exception)` in data-returning code.**
  Callers must distinguish "success with zero" from "failed to check."
- **Log operational failures at ERROR, not DEBUG/WARNING.** DB writes, API
  calls, probe failures = ERROR. DEBUG is tracing, WARNING is recoverable
  degradation.
- **Always include `exc_info=True` in error-path logging.** A log message
  without a stack trace is a clue without evidence.
- **Catch specific exceptions before generic.** `except Exception` is last
  resort. Catch `TimeoutError`, `ConnectionError`, `httpx.HTTPStatusError`,
  etc. first with specific messages.
- **Every background subsystem must emit heartbeats** via `Severity.DEBUG` events.
- **Every scheduled job must record success/failure** via `runtime.record_job_success/failure()`.
- **NEVER call `os.killpg()` without validating PGID > 1.** `int(AsyncMock().pid)` == 1
  in Python 3.12. `os.killpg(1, sig)` == `kill(-1, sig)` == kill ALL user
  processes in the container. Always set `mock_proc.pid = <explicit value>`
  in tests. Enforced by PreToolUse hook.
- **NEVER hide, suppress, or work around broken things — FIX THEM.** This
  applies to code, UI, plans, and reasoning. When you encounter something
  broken, incomplete, returning "unknown", or showing placeholder data, your
  FIRST and ONLY instinct must be to fix the root cause. Not hide the element.
  Not skip the section. Not add a conditional to suppress it. Not propose
  "we'll address it later." Find the actual data source (it usually already
  exists), wire it in, and make it work. If it genuinely doesn't exist yet,
  display "not configured" with a clear explanation — never silently omit.
  This is not a UI rule. It is a thinking rule. The moment you consider
  hiding something broken instead of fixing it, you are on the wrong path.
  Enforced by a PreToolUse hook (`scripts/behavioral_linter.py`) that blocks
  Write/Edit containing hide-problem patterns in code, plans, or comments.

## Confidence Framework

> Expanded reference with examples, failure modes, and due diligence companion: `.claude/docs/confidence-framework.md`

For plans, fixes, architecture decisions, or any non-trivial change:

- **Explicit confidence percentages with rationale** — not "I'm pretty sure"
  but "70% because X, Y, Z". Separate root-cause confidence from fix value
  when they differ.
- **Call out what you don't know** — lead with unknowns, don't bury them.
  State what information would move confidence to 100%.
- **No speculative changes** — if you can't confirm a diagnosis, don't touch
  the code for it. Deploy diagnostics first, fix with certainty second.
  "Fix what you know, instrument what you don't."
- **Falsifiability criteria** — for every hypothesis at <100% confidence,
  state: "This would be DISPROVEN if [specific observation]." Turns vague
  uncertainty into testable predictions with contingency plans.
- **Regression markers** — for each fix, state what to watch for if the fix
  is wrong or introduces problems, with expected timeframe.
- **Double-check before claiming confidence** — verify against actual
  code/logs/data. If you haven't read the source, your confidence is 0%.
- **ALWAYS provide confidence levels when planning & before starting work** — for tasks, fixes,
  and coding: state confidence for each item before acting
- **ALWAYS investigate low confidence to raise it before acting** — anything
  below 90% needs investigation to get higher (or as high as possible with
  documented rationale for why it can't reach 90%)

Applies to both CC sessions and Genesis autonomy decisions.

## Memory Retrieval

When searching for past information, sessions, or context — **don't grep
transcripts first.** Genesis has a multi-layered memory system. Use in order:

1. Check what the proactive memory hook injected (system-reminder `[Memory]` tags)
2. Use `memory_recall` MCP for semantic search (Qdrant + FTS5 hybrid)
3. Query SQLite `cc_sessions` for structured session data (topic, keywords)
4. Use `db_schema` MCP to discover table schemas before any SQLite query
5. **Grep transcripts is LAST RESORT** — only after 1-4 fail

Never guess SQLite column names — use `db_schema` first. The DB has 60+ tables.

## Rules

- **Verify before claiming done**: `ruff check . && pytest -v` minimum.
  If you can't prove it works, it's not done.
- **Test the outcome, not just the code.** Unit tests passing is necessary
  but NOT sufficient. After tests pass, verify the actual end-to-end
  outcome the change delivers. For wiring changes: verify the init/bootstrap
  order passes the right values at runtime, not just that parameters exist.
  For notification changes: verify the notification actually arrives. Ask:
  "If the system restarts right now, will this actually work?" If you can't
  answer yes with evidence, you're not done. This was learned the hard way
  on 2026-03-26: 34 tests passed, live API test passed, but a code review
  caught an init ordering bug that would have silently broken the primary
  runtime path.
- **Built ≠ wired. Wired ≠ verified.** Every component you build MUST have at
  least one call site in the actual runtime path — not just a unit test that
  mocks its callers. Before marking any component "done," answer three questions:
  (1) What calls this? Show the line. (2) What happens if that caller's input
  triggers this path? Trace it. (3) Is there an integration test that exercises
  this path end-to-end? If any answer is "nothing yet" or "it will be wired
  later," it is NOT done — it's a half-built liability. File an observation,
  leave a TODO, or wire it now. "Later" is where code goes to die.
  See the formal 4-level taxonomy (exists → substantive → wired →
  data-flow verified) in `.claude/skills/review/checklist.md`.
- **Mandatory code review after every code change.** After writing or modifying
  code, you MUST (1) run /review AND (2) dispatch the superpowers:code-reviewer
  agent — both are required before responding to the user or committing. /review
  alone is not sufficient. This is enforced by hooks — commits are blocked
  without review, and every user prompt will remind you of pending unreviewed
  changes. No exceptions.
- **Commit continuously**: after every logical unit of work. Uncommitted = lost.
  The user is the only human on this project — uncommitted work is invisible
  work, and invisible work is lost work.
- **Conventional commit prefixes**: `feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`. Scope is optional: `feat(ego): add cadence manager`.
  Keep subject line under 72 characters. Dominant category wins if mixed.
- **Check procedures before multi-step tasks**: use `procedure_recall` if relevant.
  Applies when a task involves external services, has failed before, or
  requires multi-step tool use.
- **Plan execution**: proceed through batches without pausing unless something
  goes sideways. If it does — STOP and re-plan immediately. Don't push
  through a broken approach hoping it resolves itself.
- Do NOT modify `docs/history/` unless correcting factual errors
- Architecture docs in `docs/architecture/` are the single source of truth
- **NEVER `rm -rf` the working directory.** Never run destructive commands
  without explicit user confirmation.
- **Session wrap-up**: structured handoff — what changed, what's pending, what
  was learned. If it's not committed, it doesn't exist.
- **Self-correction loop**: when the user corrects a mistake, persist the lesson
  as a concrete rule.
- **Register new capabilities**: When building a new subsystem or feature,
  register it in `GenesisRuntime.bootstrap()` by adding an entry to
  `_CAPABILITY_DESCRIPTIONS` and ensuring the init step is recorded in the
  bootstrap manifest. This writes to `~/.genesis/capabilities.json` which
  the SessionStart hook reads. Without registration, foreground sessions
  won't know the feature exists — and if it fails, they won't know it's broken.
