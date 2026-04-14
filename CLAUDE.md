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
- **Network**: Install-specific. See `~/.genesis/config/genesis.yaml` (generated
  by `scripts/setup-local-config.sh`). Dashboard proxied host:5000 → container:5000.
  only. Do NOT install locally. NOT the host VM.
- **Qdrant**: `localhost:6333` (systemd service)
- **GitHub**: configured in `~/.genesis/config/genesis.yaml` (`github.user` / `github.public_repo`)
- **Database**: `~/genesis/data/genesis.db` (NOT `~/genesis/genesis.db`)
- **Backups**: private repo, cron every 6h (`scripts/backup.sh`). Repo name in local config.
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
```

## Serena — Semantic Code Analysis

Serena is an MCP server providing LSP-powered code intelligence via Pyright.
Available as `mcp__serena__*` tools. Complements Grep, does not replace it.

**When to use Serena:**
- "Where is this class/function wired in?" → `find_referencing_symbols`
- "What's the definition and signature?" → `find_symbol` with `include_body`
- "What methods does this class have?" → `find_symbol` with `depth=1`
- Architectural traversal — dependency injection patterns, type hierarchies
- Safe refactoring — `rename_symbol`, `replace_symbol_body`

**When to use Grep instead:**
- String patterns, comments, config files, migrations, YAML/JSON
- Test files using mocks (Pyright doesn't follow mock patterns)
- Anything outside Python semantics (shell scripts, HTML templates, SQL)

**When to use the AST code index (`code_modules`/`code_symbols` tables):**
- Lightweight structural queries (module counts, symbol stats)
- Proactive hook enrichment (runs every prompt — must be fast)
- Package-level summaries

**Key behaviors:**
- 1-2s one-time LSP init per session, then fast
- Returns semantic context (symbol kind, type signatures, containing class)
- Distinguishes `TYPE_CHECKING` imports from runtime imports
- Config: `.serena/project.yml`

## Genesis Development Work

When the task involves modifying Genesis itself — fixing bugs, implementing
features, refactoring subsystems, debugging the runtime, or wiring new
components — invoke the `genesis-development` skill via the Skill tool
immediately. It contains worktree discipline, observability rules,
architecture context, the contribution pipeline, a codebase map, and an
adaptive review protocol. Do NOT load it for Genesis-as-tool work (using
Genesis to research, summarize, write content, or do non-Genesis tasks).

## Design Principles

- **Flexibility > lock-in** — Adapter patterns, generic interfaces, pluggable
  components. Every external dependency should be swappable.
- **LLM-first solutions** — Code handles structure (timeouts, validation, event
  wiring); judgment calls belong to the LLM. Prefer better prompts over
  heuristics.
- **Quality over cost — always** — Cost tracking is observability, NEVER
  automatic control. No auto-throttling or auto-degrading. The user decides
  tradeoffs; Genesis provides the levers.
- **Verify against actual code** — Docs describe intent; code describes reality.
- **CAPS markdown convention** — User-editable files that shape LLM behavior use
  UPPERCASE filenames (e.g., `SOUL.md`, `USER.md`). Transparency breeds trust.
  Additional Genesis-specific design principles (tool scoping, hook
  patterns, `$CLAUDE_PROJECT_DIR` usage) are in the `genesis-development`
  skill's `references/architecture.md`.

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

## Community Contributions

When you see a `[Contribution]` system-reminder, a post-commit hook has
detected a bug fix eligible for upstream contribution. Ask the user
conversationally — never run the pipeline without explicit approval. If
approved, invoke `genesis contribute <sha>`. If declined, do nothing.
Full pipeline details are in the `genesis-development` skill's
`references/contribution.md`.

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
  mocks its callers. "Later" is where code goes to die. The
  `genesis-development` skill has the full 4-level verification taxonomy.
- **Code review after code changes.** Dispatch the superpowers:code-reviewer
  agent after writing or modifying code. Review enforcement hooks will remind
  you of pending unreviewed changes. The `genesis-development` skill has the
  full adaptive review protocol (what level of review for what size change).
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
- **Register new capabilities**: New subsystems must be registered in the
  bootstrap manifest and capabilities file. See the `genesis-development`
  skill for specifics.
- **NEVER hide, suppress, or work around broken things — FIX THEM.** When
  you encounter something broken, your first instinct must be to fix the
  root cause. Not hide the element, not skip the section, not propose
  "we'll address it later." This is a thinking rule, not just a code rule.
- **Bugs you see get fixed or tracked — never ignored.** Every bug you
  encounter during any work must be either fixed inline or filed as a
  follow-up AND raised in your next user-facing report.

## Network Identity

Network configuration is install-specific. Run `python -m genesis.env` to display
current resolved values, or check `~/.genesis/config/genesis.yaml`.
