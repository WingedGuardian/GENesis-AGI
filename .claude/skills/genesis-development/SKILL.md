---
name: genesis-development
description: >
  This skill should be used when developing, debugging, refactoring, or
  building Genesis itself — tasks like "fix this in Genesis", "add a new
  MCP tool", "wire up the runtime", "Genesis won't start", "create a
  worktree", "debug the bridge", or "add a capability". Applies to any
  task modifying files under src/, .claude/, or tests/. Do NOT load for
  Genesis-as-tool work ("summarize this", "write a LinkedIn post",
  "research X") or general questions unrelated to Genesis internals.
consumer: cc_foreground
phase: 10
skill_type: workflow
---

## Load Gate

Before reading any reference, confirm the task is Genesis-*development*,
not Genesis-*as-tool*. If uncertain, ask the user: "Are we modifying
Genesis itself, or using Genesis for something else?"

## On-Load Mindset

Internalize these immediately when this skill fires — they shape how to
work from the start, not just what to check before commit.

### Wiring Discipline

Every new component needs at least one call site in the actual runtime
path. Apply this 4-level verification taxonomy:

1. **Exists** — file/function present. Proves nothing.
2. **Substantive** — tests pass, handles happy + error. No runtime proof.
3. **Wired** — live call site, import chain unbroken. Minimum for "done."
4. **Data-Flow Verified** — real data flows end-to-end. Required for
   critical paths.

Mark nothing "done" below Level 3.

### GROUNDWORK Code Is NOT Dead Code

Code tagged `# GROUNDWORK(feature-id): why` is intentional future
investment. Never delete or refactor it as dead code. Only remove when
the feature is fully active or the user explicitly cancels it.

### Architecture Review

For medium-to-large Genesis work (3+ files, new components, wiring
changes), dispatch a `genesis-architect` subagent before implementation
to check dependencies, edge cases, and DRY violations. Small targeted
changes skip this.

### No Silent Timeouts

Never add a timeout (`asyncio.wait_for`, `asyncio.timeout`, stream idle
timeout, subprocess timeout, watchdog threshold, etc.) without explicit
user approval. Timeouts on reflections, CC calls, cognitive paths, and
long-thinking work fight Genesis instead of helping it — they cap
legitimate long thinking and add speculative defense against rare hangs.
If a timeout is genuinely needed, surface the request to the user first
with the specific value, the failure mode it addresses, and the evidence
that the failure is real. Never build one as a "small improvement" or
"defense in depth."

### Verify Outcomes, Not Just Tests

`ruff check . && pytest -v` is the minimum bar, not the finish line.
After tests pass, verify the actual end-to-end outcome the change
delivers. Diff behavior between main and your changes when relevant.
For wiring changes: verify the init/bootstrap order passes the right
values at runtime, not just that parameters exist. For notification
changes: verify the notification actually arrives. Ask: "If the system
restarts right now, will this actually work?" If you can't answer yes
with evidence, you're not done.

### Use GitNexus for Structural Code Intelligence

GitNexus provides graph-based code intelligence that Grep/Serena cannot:
multi-hop call chains, blast radius, execution flows, coupling analysis.

**Before editing:** `gitnexus impact <symbol>` — check blast radius.
Use full UID if ambiguous (e.g., `Method:path/file.py:Class.method#N`).

**Before committing:** `gitnexus detect-changes` — verify you haven't
missed dependent symbols.

**Understanding unfamiliar code:** `gitnexus context <symbol>` for 360°
view, or browse processes via `gitnexus://repo/GENesis-AGI/processes`.

**Custom questions:** `gitnexus cypher` for raw graph queries. Note:
LadybugDB uses `CodeRelation` with a `type` property for edges, not
Neo4j-style named edge labels.

Full syntax, Cypher examples, and decision matrix:
`.claude/docs/code-intelligence-guide.md`

### Common Traps

- **Ego sessions are inert.** `src/genesis/ego/` exists but has zero
  production callers. Don't wire them; don't treat them as broken.
- **DB path confusion.** `genesis.db` is at `~/genesis/data/genesis.db`,
  NOT `~/genesis/genesis.db`. Use `genesis.env.genesis_db_path()`.
- **Column names.** Use `db_schema` MCP before assuming column names.
  The DB has 60+ tables.
- **Signal collectors.** Phase 1 built stubs; Phase 6 replaced some with
  real implementations. Code that looks complete may not produce signals.
- **Capabilities manifest.** `~/.genesis/capabilities.json` is write-once
  at bootstrap, not dynamic. New capabilities need registration in
  `_CAPABILITY_DESCRIPTIONS` in `src/genesis/runtime/_capabilities.py`
  AND a bootstrap init step.
- **APScheduler IntervalTrigger resets on restart.** `IntervalTrigger`
  counts from server startup, not from last successful run. If the
  server restarts more frequently than the interval, the job never
  fires. Use `CronTrigger` for anything longer than a few hours.
  Bit us with `user_model_evolution` (48h interval, daily restarts).

### Code Discovery

Use the right tool for how you're exploring:

- **Architecture overview** — CBM `get_architecture(aspects=["overview"])`
- **Finding symbols** — CBM `search_graph(name_pattern="...")` or Serena `find_symbol`
- **Call tracing** — CBM `trace_path(function_name="...")` or Serena `find_referencing_symbols`
- **Impact before changes** — GitNexus `impact(path="...")`
- **Config/doc/non-code files** — Grep/Read directly

Full decision matrix: `.claude/docs/code-intelligence.md`

## Adaptive Review Protocol

Choose the review level proportional to the change:

| Change type | Review level | Examples |
|---|---|---|
| Docs / text / comments | **None** | Markdown prose, inline comments |
| Simple mechanical | **None** | Variable rename, typo fix, import reorder |
| Small focused fix | **Code-reviewer agent inline** | Single-function bug fix, config tweak |
| Substantial change | **Code-reviewer inline + /review** | Multi-file refactor, new MCP tool, wiring |
| Prompt / LLM behavior | **Both + extra scrutiny** | System prompts, skill instructions, routing |

Decision criteria when ambiguous: "If the change could break a runtime
path not covered by its own unit test, it needs /review. If it only
touches things with clear, isolated test coverage, code-reviewer inline
is sufficient."

The enforcement hooks (`review_enforcement_prompt.py`,
`review_enforcement_commit.py`) still fire on every change — they are
safety nets, not the decision-maker. This protocol provides the
judgment framework.

## Pre-Commit Gate

Verify before any commit:

- `git diff --cached --stat` — every file in the diff belongs to your work
- `git status --short` — check untracked files (should be staged or ignored)
- Review level applied matches the adaptive protocol above
- Staged files do not include secrets (`secrets.env`, `.env`, credentials)
- GROUNDWORK-tagged code not accidentally deleted
- New capabilities registered in `_capabilities.py` + bootstrap manifest

## Reference Router

Read references ONLY when relevant to the specific task. Do NOT load all
references on every trigger.

| When you need... | Read... |
|---|---|
| Codebase structure, package map, gotchas, debugging | `references/codebase-map.md` |
| Package/module/symbol navigation (progressive drill) | `codebase_navigate` MCP tool (L0→L1→L2) |
| venv, DB paths, Qdrant, Ollama, network, commands | `references/environment.md` |
| Worktree rules, concurrent sessions, branch naming | `references/worktrees.md` |
| tracked_task, exc_info, os.killpg, logging patterns | `references/observability.md` |
| V3 state, build order, GROUNDWORK, architecture docs | `references/architecture.md` |
| Phase 6 contribution pipeline, sanitizer | `references/contribution.md` |
| Pending work, active incidents, subsystem status | `references/build-state.md` |
| Which code tool to use (CBM vs Serena vs GitNexus vs Grep) | `.claude/docs/code-intelligence.md` |

**Freshness rule:** On first read of `codebase-map.md` in a session,
verify structural claims against current code. If a package status or
gotcha has changed, flag to user before acting on stale assumptions.

## Public Repo & Release Workflow

The public repo (`GENesis-AGI`) is the primary development repo.
Standard open-source workflow: PRs go directly to the public repo.

- **Squash merges only** — merge commits are disabled on the public repo.
  Always `git pull --rebase origin main` after merging a PR before
  committing locally, or push will be rejected (non-fast-forward).
- **README is public-authoritative** — the public repo's `README.md` is
  hand-crafted and must NEVER be overwritten.
- **CHANGELOG audience is users** — only include entries a user updating
  their install would care about. No internal refactors, README changes,
  CI tweaks, or process artifacts. Lead with the user-visible effect, not
  the implementation technique.
- **No sensitive data in commits** — voice data, research profiles, IPs,
  and secrets must never enter the repo. User data lives in overlays
  outside the repo (e.g., `~/.claude/skills/*/`, `~/.genesis/`).
