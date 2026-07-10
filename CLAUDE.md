# Genesis v3 — Project Instructions

Genesis v3 is an autonomous AI agent system.

## Architecture

Channels (Telegram, Dashboard, OpenClaw) → Cognitive Core (CCInvoker, triage,
reflection) → Services (routing, memory, outreach, autonomy, surplus) → Data
(SQLite WAL, Qdrant, ~/.genesis/) → Observability (event bus, health).
89 packages, ~192K LOC. Use `codebase_navigate` MCP to explore.

## Environment

- **Python**: 3.12 (venv at `~/genesis/.venv`)
- **Node**: 22.x
- **Host VM**: Configured in `~/.genesis/guardian_remote.yaml` (set by
  `install_guardian.sh`). Guardian runs here. SSH access is Guardian-only
  via the `guardian-gateway.sh` command dispatcher. NOT the Ollama server.
- **Network**: Install-specific. See `~/.genesis/config/genesis.yaml` (generated
  by `scripts/setup-local-config.sh`). Dashboard proxied host:5000 → container:5000.
- **Qdrant**: `localhost:6333` (systemd service)
- **GitHub**: configured in `~/.genesis/config/genesis.yaml` (`github.user` / `github.public_repo`)
- **Database**: `~/genesis/data/genesis.db` (NOT `~/genesis/genesis.db`)
- **Backups**: encrypted, every 6h via `genesis-backup.timer` (systemd user
  unit; enable deliberately after configuring) running `scripts/backup.sh` → your private
  `genesis-backups` repo (SQLite, Qdrant, memory, transcripts, config,
  secrets). Restore via `scripts/restore.sh` or `python -m genesis restore`.
- **Env scrub**: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is NOT used — Genesis
  hooks and MCP servers require inherited API keys (DeepInfra, Qwen, etc.).
- **Setup**: `./scripts/bootstrap.sh` (venv, config, services, memory)
- **Temp files**: `~/tmp/` for transient files and any LARGE temp (downloads,
  media, DB dumps, exports). NEVER write large files to `/tmp/` (a small
  tmpfs/RAM) or `~/.genesis/cc-tmp/` — the latter is Claude Code's working temp
  ("oxygen"), policed by the `genesis-tmp-watchgod` service, which **kills CC
  sessions** when it fills. A CC session's `TMPDIR` points at `cc-tmp` by design;
  do NOT override `TMPDIR` in scripts or service files (breaks CC — see the
  `tmp_filesystem_limit` procedure). Code that creates large temp must pass an
  explicit dir (`mktemp -p ~/tmp` / `tempfile(dir=…)`), never the default. For a
  heavy one-off you run interactively, prefix `TMPDIR=~/tmp/job <cmd>`. Clean up
  after use.

## Process Management

All Genesis services are **systemd user units** (`~/.config/systemd/user/`).
NEVER use `nohup` or bare `python -m genesis serve` — a bare process holds
the lock file and blocks the systemd unit.

```bash
systemctl --user restart genesis-server          # Restart (NEVER nohup)
systemctl --user status genesis-server           # Check
journalctl --user -u genesis-server -n 50        # Logs
systemctl --user list-units 'genesis-*' --all    # All units
```

Other units: `genesis-bridge.service` (LEGACY fallback — full stack incl.
Telegram, only when genesis-server is DOWN; it yields/exits 200 if the server
lock is held, and must never run alongside the server — dual getUpdates
pollers split updates and break approval buttons),
`genesis-tmp-watchgod.service` (/tmp protection), `genesis-watchdog.timer`
(health check), `genesis-backup.timer` (6h encrypted backup via
`scripts/backup.sh`), `genesis-disk-hygiene.timer` (daily worktree reaping, cache reclaim, `~/tmp`
prune, and label-aware attention-snapshot GC; see `scripts/disk_hygiene.sh`). MCP servers are CC child processes
(not systemd) — code changes take effect on next CC session start.

## Common Commands

```bash
source ~/genesis/.venv/bin/activate               # Required for all Python work
cd ~/genesis && ruff check .                      # Lint all Python
pytest tests/test_memory/test_drift.py -v         # Targeted tests (ALWAYS specify file)
gh pr checks <PR-number>                          # CI results (replaces local full suite)
curl -s http://localhost:6333/collections | jq .  # Verify Qdrant
systemctl --user restart genesis-server           # Restart server (NEVER nohup)
systemctl --user status genesis-server            # Verify server running
```

## Code Intelligence

Pick the tool by the question (full matrix + freshness model:
`.claude/docs/code-intelligence.md`): **Grep/Glob/Read** for text/configs;
**Serena** (Python LSP) for symbols/references/rename — **always live**, the
default for "who calls X / what breaks if I change Z"; **codebase-memory-mcp**
for architecture/graph; **GitNexus** for deep blast-radius/flows/coupling —
**snapshot-based, so `gitnexus analyze` first** when freshness matters (it
drifts after pulling merged PRs). Prefer these over manual reads for dependency
questions; none is a mandatory pre-edit gate.

## Skill Library

Tier 1 skills (`.claude/skills/`) are always indexed. Additional specialized
skills live in `src/genesis/skills/` and `~/.genesis/skill-library/` — browse
these when a task would benefit from a structured approach (research, outreach,
browser automation, content, etc.). The skill injection hook nudges you when
one matches.

## Web Tools

**Default to MCP tools** (`web_fetch`, `web_search`) — they handle anti-bot,
work in background sessions, and return structured data. CC built-in
`WebFetch`/`WebSearch` are fine for quick lookups or when AI summarization
is the goal. Full decision guide: `.claude/docs/web-tools-guide.md`

## Background Sessions

Task > 20 min or needs persistent writes → **background session**. Quick
research → **sub-agent**. Profiles: `observe` · `interact` · `research`.
Full guide: `.claude/docs/background-sessions.md`

## Genesis Development Work

When the task involves modifying Genesis itself — fixing bugs, implementing
features, refactoring subsystems, debugging the runtime, or wiring new
components — invoke the `genesis-development` skill via the Skill tool
immediately. Do NOT load it for Genesis-as-tool work (using
Genesis to research, summarize, write content, or do non-Genesis tasks).

## Vision

- **Philosophical foundation**: `docs/architecture/genesis-v3-vision.md` —
  Genesis's self-understanding, purpose, and aspirations. This is your "why."

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
- **Cognitive architecture is not a service** — Genesis's LLM call sites,
  routing chains, and extraction pipelines serve its own cognitive processes
  (memory, reflection, triage, learning). Module work uses external tools and
  APIs, not Genesis internals. Genesis provides research capabilities (search,
  fetch, crawl) but its thinking infrastructure is its own.
- Additional Genesis-specific design principles (tool scoping, hook
  patterns, `$CLAUDE_PROJECT_DIR` usage) are in the `genesis-development`
  skill's `references/architecture.md`.

## Your Genesis

Your Genesis install is one operational system: the public `GENesis-AGI`
codebase (the cognitive core), the public `GENesis-Voice` repo for
voice/edge-device software (HAOS Voice PE device firmware, esphome configs, S2S/
ambient audio bridges, edge deploy — `GENesis-AGI` keeps only its internal
channel code), your private fork for customizations, and your private
`genesis-backups` repo for encrypted data. Full model:
`.claude/docs/your-genesis.md`.

Background session transcripts (reflections, inbox, surplus) are stored
under `~/.genesis/background-sessions/` (outside the repo, so CC's resume
picker doesn't include them).

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

**Two memory systems:** a **fact, decision, or plan** to store for later →
*Genesis memory* (`memory_store` MCP, system-wide). Something that must **affect
behavior during the conversation** → *CC file memory* (`~/.claude/.../memory/`,
foreground-only). Unsure → ask.

Genesis memory operates in 4 layers. Each has a role — use the lightest layer
that answers your question before escalating.

**L1 — Essential Knowledge (always present, ~150-300 tokens):**
`~/.genesis/essential_knowledge.md` — active context, recent decisions, wing
index. If this answers "what are we working on," don't burn a recall.

**L2 — Proactive Recall (automatic per prompt):**
The UserPromptSubmit hook searches FTS5 + Qdrant based on your prompt keywords
and injects `[Memory | age | wing | id:xxx]` tags (300/200 chars, rank 1/2-3).
Check these first before doing explicit recall. Results are biased toward the
active wing (domain) when detectable. Use the `id:` handle with `memory_expand`
for full context without re-searching. Proactive hook results are keyword-matched
fragments, not curated
context. They may be ambiguous, conditional, or outdated when detached from
their source document. Treat them as leads to investigate, not facts to act on.
When a memory snippet makes a factual claim (X is broken, Y is exhausted, Z is
deprecated), verify before incorporating into your reasoning.

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

## Memory Recall Behavior

- **Search the whole system.** Use `memory_recall` with `source='both'`
  to search episodic AND knowledge_base. Don't assume episodic alone is
  sufficient. For domain-specific topics (external tools, products, APIs),
  also try `knowledge_recall` with the product/tool name.
- **Distinguish first-party from external-world.** Recall results carry a
  `provenance` label: `first-party memory` (Genesis's own observations,
  decisions, conversations) vs `external-world knowledge (source: …)` (the
  knowledge base, ingested docs, corrective web results). Never treat
  external-world knowledge as first-party ground truth — weigh it as
  information about the world. The proactive hook shows the same split
  inline (`[KB·<source>]` vs `[Memory]`).
- **Follow surfaced procedures.** When a `[Procedure]` tag appears in
  proactive results, read the full procedure via `procedure_recall`,
  evaluate applicability (>80% match = follow it), and note deviations.
  Update via `procedure_store` if the procedure is outdated.
- **Expand related memory hints.** When proactive results show
  `[→ related: id:xxx]`, use `memory_expand` to get full context when the
  topic is actively relevant.
- **Don't wait to be asked.** When a topic comes up that likely has prior
  context (recurring themes, named entities, project references),
  proactively recall before responding. The user should not have to say
  "check memory."

## MCP Tool Selection

When multiple Genesis MCP tools could handle a task, see
`.claude/docs/mcp-tools-guide.md` for decision trees (storage taxonomy,
recall taxonomy, health debugging escalation).

## Reference Capture

Credentials, URLs, IPs, and identifiers shared in conversation are
auto-stored via `reference_store`. Retrieve with `reference_lookup` or
`knowledge_recall(domain='reference.*')`. Human view: the dashboard
**References** tab (browse/search/reveal/delete, live against the store).

**Real-time capture is your responsibility.** When you create an account,
receive credentials, generate API keys, or encounter any login/token/secret
in conversation, call `reference_store` immediately — don't rely on batch
extraction to catch it later. You are the first line of defense; the
extraction pipeline is the safety net.

## Knowledge Ingestion (Conversational Path)

When a user shares a file path or URL in conversation:
- If they explicitly ask to ingest/store/learn it: confirm project_type
  and domain, then call `knowledge_ingest_source` MCP tool.
- If the context is ambiguous: ask "Would you like me to store this to
  the knowledge base as an authoritative source?"
- Never auto-ingest without explicit user confirmation.
- The dashboard also supports drag-drop file upload on the Knowledge tab.

## Traps

- **Ego** (`src/genesis/ego/`) — Live. Two egos: user (CEO/Opus) and
  Genesis (COO/Sonnet). Review cadence manager and budget controls
  before adding call sites.
- **GROUNDWORK tags** — `# GROUNDWORK(id): why` is intentional. Never delete.
- **IntervalTrigger** — Resets on restart. Use `CronTrigger` for intervals >1h.

## Rules

- **Output files go outside the repo.** Write to `~/.genesis/output/`,
  never into the repo tree.
- **Execute, don't delegate.** Perform actions directly instead of
  telling the user to run terminal commands. Exception: irreversible,
  financial, or destructive actions need explicit approval first.
- **No unsanctioned financial transactions.** Every transaction needs
  explicit user approval, every time. Prior approval does not carry forward.
- **Timeout policy.** Justify any timeout with a specific failure mode.
  Default floor: 2 hours (7200s). Full policy in genesis-development skill.
- **Verify outcomes, not just tests.** "If the system restarts now, will
  this work?" Built ≠ wired ≠ verified. Details in genesis-development skill.
- **Code review after code changes.** Codex will review your output.
  Protocol in genesis-development skill.
- **Commit continuously**: uncommitted = invisible = lost.
- **Procedure recall is automatic** — the proactive hook surfaces relevant
  procedures. Store new procedures immediately when you discover them.
- **Never insert directly into `task_states`.** Use `task_submit` MCP
  after `/task` intake.
- **Never pipe background Bash commands.** `run_in_background` with pipes
  produces empty output. Run without pipes or in foreground.
- **Plan mode by default** for any task with 3+ steps or architectural
  decisions. If something goes sideways — STOP and re-plan.
- **Use subagents** to keep main context clean. One concern per subagent.
- **NEVER `rm -rf` the working directory.** Never run destructive commands
  without explicit user confirmation.
- **Session wrap-up**: structured handoff — what changed, what's pending,
  what was learned. If it's not committed, it doesn't exist.
- **Follow-up discipline**: bias = FIX NOW, not defer. A follow-up is valid
  ONLY if the work is (1) blocked on a precondition unmet this session (incl.
  an unmade design decision), (2) gated on time/data, or (3) big enough to
  derail this session — or the user directs it as separate. Otherwise do it
  now, even if unrelated/unasked; "already noted in a PR/comment" is not a
  reason to also create a row. Valid ones: create via `follow_up_create` MCP,
  never leave as just text. **Two lanes** (`kind` on a follow-up): `follow_up`
  = committed work, actionable, dispatched/surfaced (the FIX-NOW-or-valid-defer
  cases above); `tabled` = an awareness record — "tracked, keep a record, not
  acting near-term" — bug-tracker semantics, never dispatched or surfaced as
  action. Genuine someday/maybe and deferred known bugs go to `tabled`, not a
  `follow_up` row. (Inbox WATCH/BOOKMARK markers auto-route to `tabled` and
  soft-decay after 60d.)
- **No laziness.** Find root causes. No temporary fixes. No shortcuts.
  Don't EVER mute the symptom — fix the problem.
- **Read before writing.** Never modify code you haven't fully read.
  Don't assume what a function does based on its name.
- **Self-correction loop**: persist lessons as concrete rules that PREVENT
  mistakes, not just document them.
- **NEVER hide broken things — FIX THEM.** Fix the root cause, not the
  symptom. This is a thinking rule, not just a code rule.
- **Bugs you see get fixed or tracked — never ignored.** Fix now by default; a
  bug you consciously defer becomes a `tabled` record (bug-tracker lane above),
  never a silent drop.
- **Telegram reminders**: use `outreach_send` with `preferred_timing`,
  NOT the `/schedule` skill (that's Claude Code's remote scheduler).
- **Cognitive co-pilot, not order taker.** On every task, ask: "what else
  is wrong here that nobody asked about?" Surface it. Don't just execute
  the stated request — find related issues, challenge assumptions, suggest
  what the user hasn't thought of. The value of Genesis is anticipation,
  not compliance. If you catch yourself just doing exactly what was asked
  and nothing more, you're underperforming. And treat the user's examples as
  a sample, not the spec: when they name a few instances, enumerate and probe
  the broader class yourself instead of spiking only the named cases —
  "just a couple of examples" is always implied.
- Dev-specific rules (commit prefixes, targeted tests, push/PR workflow,
  capability registration) are in the genesis-development skill.
