# Genesis v3 — Project Instructions

Genesis v3 is an autonomous AI agent system.

## Environment

- **Python**: 3.12 (venv at `~/genesis/.venv`)
- **Node**: 22.x
- **Host VM**: Configured in `~/.genesis/guardian_remote.yaml` (set by
  `install_guardian.sh`). Guardian runs here. SSH access is Guardian-only
  via the `guardian-gateway.sh` command dispatcher. NOT the Ollama server.
- **Network**: Install-specific. See `~/.genesis/config/genesis.yaml` (generated
  by `scripts/setup-local-config.sh`). Dashboard proxied host:5000 → container:5000.
  only. Do NOT install locally. NOT the host VM.
- **Qdrant**: `localhost:6333` (systemd service)
- **GitHub**: configured in `~/.genesis/config/genesis.yaml` (`github.user` / `github.public_repo`)
- **Database**: `~/genesis/data/genesis.db` (NOT `~/genesis/genesis.db`)
- **Backups**: encrypted 6h cron via `scripts/backup.sh` → your private
  `genesis-backups` repo (SQLite, Qdrant, memory, transcripts, config,
  secrets). Restore via `scripts/restore.sh` or `python -m genesis restore`.
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

Use for symbol references, definitions, type hierarchies, and safe refactoring.
Use Grep instead for non-Python (config, YAML, shell, SQL, mocks).
Full decision guide: `.claude/docs/serena-guide.md`

## Genesis Development Work

When the task involves modifying Genesis itself — fixing bugs, implementing
features, refactoring subsystems, debugging the runtime, or wiring new
components — invoke the `genesis-development` skill via the Skill tool
immediately. Do NOT load it for Genesis-as-tool work (using
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

## Your Genesis

Your Genesis install is one operational system: the public `GENesis-AGI`
codebase, your private fork for customizations, and your private
`genesis-backups` repo for encrypted data. Full model:
`.claude/docs/your-genesis.md`.

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
  the code for it.
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

**L1 — Essential Knowledge (always present, ~150-300 tokens):**
`~/.genesis/essential_knowledge.md` — active context, recent decisions, wing
index. If this answers "what are we working on," don't burn a recall.

**L2 — Proactive Recall (automatic per prompt):**
The UserPromptSubmit hook searches FTS5 + Qdrant based on your prompt keywords
and injects `[Memory]` tags. Check these first before doing explicit recall.
Results are biased toward the active wing (domain) when detectable.

**L3 — Deep Search (on demand):**
Use `memory_recall` MCP for full hybrid retrieval. Use when L1-L2 don't answer
the question. Query SQLite `cc_sessions` for structured session data. Use
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
autonomy.

## Reference Capture

When user input contains credentials, URLs, IP addresses, account handles,
or other unique identifiers with context, Genesis automatically stores them
via `reference_store` with a description of what they're for.

**Retrieval**: use `reference_lookup(query, kind=...)` or
`knowledge_recall(domain='reference.*')`. Proactive memory hook surfaces
matches automatically.

**Human view**: `~/.genesis/known-to-genesis.md` is a read-only mirror,
regenerated on every `reference_store` call. Browse to see everything
Genesis has learned.

**secrets.env is orthogonal**: `secrets.env` is for Genesis's own
infrastructure credentials. The reference
store is for user-level credentials and reference data that Genesis learns
across sessions.

## Knowledge Ingestion (Conversational Path)

When a user shares a file path or URL in conversation:
- If they explicitly ask to ingest/store/learn it: confirm project_type
  and domain, then call `knowledge_ingest_source` MCP tool.
- If the context is ambiguous: ask "Would you like me to store this to
  the knowledge base as an authoritative source?"
- Never auto-ingest without explicit user confirmation.
- The dashboard also supports drag-drop file upload on the Knowledge tab.

## Community Contributions

When you see a `[Contribution]` system-reminder, a post-commit hook has
detected a bug fix eligible for upstream contribution. Ask the user
conversationally — never run the pipeline without explicit approval. If
approved, invoke `genesis contribute <sha>`. If declined, do nothing.
Full pipeline details are in the `genesis-development` skill's
`references/contribution.md`.

## Scheduling Reminders

To send the user a future Telegram reminder, use `mcp__genesis-outreach__outreach_send` with `preferred_timing` set to an ISO timestamp. This persists in the DB and fires via the Genesis outreach pipeline. Do NOT use the `/schedule` skill — that routes to Claude Code's remote cloud scheduler, not Genesis.

## Rules

- **Execute, don't delegate.** When Genesis has API or exec access to a
  system (local or remote), perform the action directly instead of
  telling the user to run terminal commands. If unsure whether to act,
  ask "Want me to handle this?" — never silently list commands and
  expect the user to copy-paste. The exception: irreversible, financial,
  or destructive actions need explicit approval first. The user's role
  is strategy and decisions; Genesis's role is execution.
- **No unsanctioned financial transactions.** Genesis must NEVER send
  money, transfer credits, or initiate any financial transaction without
  explicit user approval — every single time, for every transaction.
  Prior approval does not carry forward. The only exception is a
  dedicated account the user has explicitly authorized for autonomous
  spending within stated limits.
- **No silent timeouts.** Never add a timeout (`asyncio.wait_for`,
  `asyncio.timeout`, stream idle timeout, subprocess timeout, watchdog
  threshold, etc.) to Genesis without explicit user approval.
- **Verify the outcome, not just the tests.** End-to-end verification
  required — "if the system restarts now, will this work?" Details in
  genesis-development skill.
- **Built ≠ wired. Wired ≠ verified.** Every component needs a live call
  site in the actual runtime path, not just a unit test. Taxonomy in
  genesis-development skill.
- **Code review after code changes.** Dispatch superpowers:code-reviewer.
  Protocol in genesis-development skill.
- **Codex will review your output once you finish.**
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
- **Follow-up ownership**: For each follow-up item identified during a session:
  1. State what it is and why
  2. State what Genesis will do: schedule it, flag for ego, or surface to user
  3. Create the follow-up via the `follow_up_create` MCP tool before session ends
  4. Never leave a follow-up as just text — every deferred item needs a backing
     record. Genesis owns follow-through, not the user.
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
- **Register new capabilities** in bootstrap manifest + capabilities file.
- **NEVER hide, suppress, or work around broken things — FIX THEM.** When
  you encounter something broken, your first instinct must be to fix the
  root cause. Not hide the element, not skip the section, not propose
  "we'll address it later." This is a thinking rule, not just a code rule.
- **Bugs you see get fixed or tracked — never ignored.** Every bug you
  encounter during any work must be either fixed inline or filed as a
  follow-up AND raised in your next user-facing report.
