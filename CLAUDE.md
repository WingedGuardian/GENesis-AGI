# Genesis v3 — Project Instructions

Genesis v3 is an autonomous AI agent system.

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
- **Network**: See `## Network Identity` section at bottom of this file.
  Set by install scripts. Dashboard proxied host:5000 → container:5000.
  only. Do NOT install locally. NOT the host VM.
- **Qdrant**: `localhost:6333` (systemd service)
- **GitHub**: `YOUR_GITHUB_USER/Genesis`
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
python -m genesis serve                           # Standalone server (port 5000)
python -m genesis serve --port 5001               # Custom port
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
(reflections, inbox, surplus) are stored under `~/.genesis/background-sessions/`
(outside the repo, so CC's resume picker doesn't include them).

## Key Architecture Documents

1. `docs/architecture/genesis-v3-vision.md` — Core philosophy and identity
2. `docs/architecture/genesis-v3-autonomous-behavior-design.md` — Primary design
3. `docs/architecture/genesis-v3-build-phases.md` — Safety-ordered build plan
4. `docs/architecture/genesis-v3-dual-engine-plan.md` — Multi-engine strategy
5. `docs/architecture/genesis-v3-gap-assessment.md` — Pre-implementation risks

## Hosting Modes

Genesis runs standalone: `python -m genesis serve` starts the full runtime
(dashboard, Telegram, OpenClaw endpoint).

**OpenClaw** is supported as a channel gateway. Genesis exposes
`POST /v1/chat/completions` so OpenClaw can route 20+ channels through it.
Config example: `config/openclaw-example.json5`.

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
- **Bugs you see get fixed or tracked — never ignored.** Every bug you
  encounter during any work (even unrelated work, pre-existing bugs,
  things mentioned in passing) must be either fixed inline (if small
  and low-risk) or filed as a follow-up (observation, task, TODO) AND
  raised in your next user-facing report. "Out of scope" is not an
  option. Ask if you're unsure which path fits; the only wrong move
  is silence.

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

## Memory System — Layer Model

Genesis memory operates in 4 layers. Each has a role — use the lightest layer
that answers your question before escalating.

**L0 — Identity (always present, ~200 tokens):**
SOUL.md + USER.md injected at session start. Who Genesis is, who the user is.
You don't need to do anything — this is automatic.

**L1 — Essential Knowledge (always present, ~150-300 tokens):**
`~/.genesis/essential_knowledge.md` injected at session start. Contains: active
context, recent decisions, wing index. Regenerated after each foreground session.
If this answers your question about "what are we working on" or "what was
decided recently," you're done — don't burn a recall.

**L2 — Proactive Recall (automatic per prompt):**
The UserPromptSubmit hook searches FTS5 + Qdrant based on your prompt keywords
and injects `[Memory]` tags. Check these first before doing explicit recall.
Results are biased toward the active wing (domain) when detectable.

**L3 — Deep Search (on demand):**
Use `memory_recall` MCP for full hybrid retrieval (vector + FTS5 + RRF fusion
+ activation scoring + graph traversal). Use when L1-L2 don't answer the
question. Query SQLite `cc_sessions` for structured session data. Use
`db_schema` MCP to discover table schemas before any SQLite query (60+ tables).
**Grep transcripts is LAST RESORT** — only after all above fail.

**When to store back:**
If you synthesize an answer from multiple recalled memories — something that
connects information in a new way — store it via `memory_store` with
`tags: ["synthesis"]` and appropriate wing/room tags. This is how the memory
system compounds over time. Don't store routine answers; store genuine syntheses
that would be expensive to re-derive.

**Wings (structural domains):**
Memories are tagged with a `wing` (top-level domain) and optional `room`
(specific topic). When searching, you can filter by wing for domain-specific
recall. Current wings: memory, learning, routing, infrastructure, channels,
autonomy. The proactive hook auto-detects your active wing from file paths
and prompt keywords.

## Community Contribution Offers (Phase 6)

When the user commits a bug fix (`fix:` conventional commit), a post-commit
git hook writes a marker file under `~/.genesis/pending-offers/`. On the next
user prompt, the `contribution_offer_hook.py` UserPromptSubmit hook injects a
`[Contribution]` system-reminder so Genesis can proactively offer: "Want to
contribute this fix upstream to the public Genesis repo?"

**Your job** when you see a `[Contribution]` reminder:
1. Ask the user conversationally — do not run the pipeline without explicit approval
2. If yes, invoke `genesis contribute <sha>` as a `Bash` tool call. The CLI
   handles: divergence check → version gate → sanitizer → adversarial review →
   consent prompt → draft PR via `gh`
3. If the user declines or ignores, do nothing — the marker has already been
   unlinked by the hook

**Opt-out:** committing with `fix(local):` scope (e.g. `fix(local): tweak
my voice exemplars`) skips the offer entirely. Standard conventional-commits
idiom for "this is local noise, don't publish it."

**What ships in every PR body** (generated by the pipeline, not you):
- Contributor install version (`<version>@<short-sha>`)
- Version status (matches upstream HEAD, or N commits behind)
- Install ID (first 8 chars of local UUID — pseudonymous by default)
- Sanitizer finding count + scanners run (`detect-secrets`, `portability`, ...)
- Review result (first-success chain: `codex` → `cc-reviewer` → genesis-native)

**Attribution:** pseudonym by default (`contributor-<id>@genesis.local`). Pass
`--identify` to use the user's real git identity.

**MVP scope:** bug fixes only. New features are out of scope for Phase 6.1.
Use `--allow-non-fix` only if explicitly asked.

The contribution pipeline is **fail-closed** — the sanitizer refuses any diff
that contains secrets, personal email addresses, hardcoded IPs, `/home/ubuntu`
paths, or files on the `contribution_forbidden` tier of
`config/protected_paths.yaml`. If a sanitizer block looks like a false
positive, the right response is to fix the diff locally, not to pressure the
pipeline.

## Rules

- **No silent timeouts.** Never add a new timeout (`asyncio.wait_for`,
  `asyncio.timeout`, stream idle timeout, subprocess timeout, watchdog
  threshold, etc.) to Genesis without explicit user approval. Timeouts
  on reflections, CC calls, cognitive paths, and long-thinking work fight
  Genesis instead of helping it — they cap legitimate long thinking and
  add speculative defense against rare hangs. If a timeout is genuinely
  needed, surface the request to the user first with the specific value,
  the failure mode it addresses, and the evidence that the failure is
  real. Never build one as a "small improvement" or "defense in depth."
- **Verify the outcome, not just the tests.** `ruff check . && pytest -v` is
  the minimum bar, not the finish line. After tests pass, verify the actual
  end-to-end outcome the change delivers. Diff behavior between main and your
  changes when relevant. For wiring changes: verify the init/bootstrap order
  passes the right values at runtime, not just that parameters exist. For
  notification changes: verify the notification actually arrives. Ask: "If the
  system restarts right now, will this actually work?" If you can't answer yes
  with evidence, you're not done. Learned the hard way on 2026-03-26: 34 tests
  passed, live API test passed, but a code review caught an init ordering bug
  that would have silently broken the primary runtime path.
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
- **Plan mode by default.** Enter plan mode for any task with 3+ steps or
  architectural decisions. Plan verification steps, not just build steps. If
  something goes sideways mid-execution — STOP and re-plan immediately. Don't
  push through a broken approach hoping it resolves itself.
- **Use subagents to keep main context clean.** Offload research, exploration,
  and parallel analysis to subagents. One concern per subagent for focused
  execution. For complex problems, throw more compute at it rather than
  cramming everything into the main context window.
- Do NOT modify `docs/history/` unless correcting factual errors
- Architecture docs in `docs/architecture/` are the single source of truth
- **NEVER `rm -rf` the working directory.** Never run destructive commands
  without explicit user confirmation.
- **Session wrap-up**: structured handoff — what changed, what's pending, what
  was learned. If it's not committed, it doesn't exist.
- **No laziness.** Find root causes. No temporary fixes. No "good enough"
  shortcuts. No skipping steps because the answer seems obvious. Hold yourself
  to senior developer standards — if you wouldn't approve it in a code review,
  don't write it. When you feel the pull to take a shortcut, that's the moment
  to slow down and do it properly.
- **Read before writing.** Never modify code you haven't fully read. Don't
  assume what a function does based on its name — read the implementation.
  Don't edit a file based on a grep match — read the surrounding context.
  Wrong assumptions from skimming produce wrong fixes.
- **Self-correction loop**: when the user corrects a mistake, persist the lesson
  as a concrete rule — one that PREVENTS the mistake, not just documents it.
  Ruthlessly iterate on these lessons until the mistake rate drops. Review
  relevant lessons at session start (the memory system surfaces these
  automatically — read them, don't skip them).
- **Register new capabilities**: When building a new subsystem or feature,
  register it by adding an entry to `_CAPABILITY_DESCRIPTIONS` in
  `src/genesis/runtime/_capabilities.py` and ensuring the init step is
  recorded in the bootstrap manifest (via `_run_init_step` /
  `_run_init_step_async` in `GenesisRuntime.bootstrap()`). This writes to
  `~/.genesis/capabilities.json` which the SessionStart hook reads. Without
  registration, foreground sessions won't know the feature exists — and if
  it fails, they won't know it's broken.

## Network Identity

- **Container IP**: ${CONTAINER_IP:-localhost} (v6: ${CONTAINER_IPV6:-not configured})
- **Host VM IP**: ${VM_HOST:-localhost}
- **Dashboard**: http://${VM_HOST:-localhost}:5000 (via proxy device)
