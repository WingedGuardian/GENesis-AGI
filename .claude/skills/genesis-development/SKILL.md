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

### Timeout Policy

The burden of proof is on you to justify why a timeout should exist.
Do not default to "add a timeout for safety." Instead:

1. **Identify the specific failure mode.** What hangs? Why? Is there
   evidence this actually happens, or is it speculative?
2. **Justify the specific value.** Why this number and not another?
   What legitimate work would be killed at a lower value?
3. **If you have no strong justification for a specific value, default
   to 2 hours (7200s).** This is the project floor — generous enough to
   never interfere with legitimate work while preventing permanent
   resource lockout from truly hung processes.
4. **Surface the request to the user** with the value, the failure mode,
   and the evidence. Never add a timeout as a "small improvement" or
   "defense in depth."

Timeouts on reflections, CC calls, cognitive paths, and long-thinking
work fight Genesis instead of helping it — they cap legitimate long
thinking and add speculative defense against rare hangs. The exception
is raw subprocess calls with no external watchdog (e.g., deterministic
executor steps), where a hung process blocks shared resources (executor
semaphore) with no other recovery mechanism.

### Verify Outcomes, Not Just Tests

`ruff check . && pytest -v` is the minimum bar, not the finish line.
After tests pass, verify the actual end-to-end outcome the change
delivers. Diff behavior between main and your changes when relevant.
For wiring changes: verify the init/bootstrap order passes the right
values at runtime, not just that parameters exist. For notification
changes: verify the notification actually arrives. Ask: "If the system
restarts right now, will this actually work?" If you can't answer yes
with evidence, you're not done.

### Code Intelligence — pick the right lane

**Serena (Python LSP) is always live** — it parses current files per query, so
it's the default for symbol/reference/impact questions ("who calls X", "what
breaks if I change Z") and never goes stale. CBM gives the architecture/graph
overview. GitNexus does what neither can — multi-hop blast radius, execution
flows, route/tool maps, coupling/community analysis — but it is **snapshot-
based**: its answers are only correct when the index matches the working tree,
and it drifts after you pull merged PRs (its reindex fires on local commit, not
on pull). So reach for GitNexus deliberately for its unique views, and run
**`gitnexus analyze` first** when freshness matters; for live "who calls this"
during active editing, prefer Serena. There is no "always run impact before
every edit" mandate — that just gates work behind a tool that's stale-by-design.

- **Blast radius / impact:** Serena `find_referencing_symbols` (live) for the
  direct caller set; GitNexus `impact <symbol>` (reindex first) for multi-hop +
  affected processes/risk. Use the full UID if ambiguous
  (`Method:path/file.py:Class.method#N`).
- **Unfamiliar code:** `gitnexus context <symbol>` or browse
  `gitnexus://repo/GENesis-AGI/processes` (when fresh).
- **Custom questions:** `gitnexus cypher` — LadybugDB uses `CodeRelation` with a
  `type` property for edges, not Neo4j-style named edge labels.

Full syntax and Cypher examples: `.claude/docs/code-intelligence-guide.md`;
tool-selection decision matrix: `.claude/docs/code-intelligence.md`

### Common Traps

- **Ego sessions are ACTIVE.** `src/genesis/ego/` is live (v3.0a11).
  Two egos: user ego (CEO, Opus) and Genesis ego (COO, Sonnet). Both
  run on adaptive cadence via the awareness loop. Changes here are
  production changes.
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
- **Modules are NEVER subsystems.** A capability *module*
  (`src/genesis/modules/**`, an external pluggable capability — "hands,
  not brain", see `modules/base.py`) is not an internal Genesis
  *subsystem* (memory, reflection, ego, triage, autonomy, sentinel).
  Module memory writes must **never** set a `source_subsystem` value —
  that tag means "internal decisional output, exclude from default
  recall", which is wrong for module output. This is enforced
  mechanically: any `.store()` under `modules/**` passing
  `source_subsystem` is a hard CI failure in
  `tests/test_memory/test_store_subsystem_coverage.py`, which also forces
  every new memory-writer to either tag itself or be explicitly
  classified as user-context. `_KNOWN_SUBSYSTEMS`
  (`memory/retrieval.py`) is the authoritative subsystem list; adding a
  module name to it is a category error.
- **Destructive data migrations must reconcile cross-store mirror fields.**
  When a cleanup/backfill deletes data in one store (e.g. Qdrant vectors) but
  another store mirrors that data's existence (e.g.
  `memory_metadata.embedding_status`), the delete MUST also fix the mirror
  field. A deleted vector left as `embedding_status='embedded'` is a field
  that *lies*, and that lie is not cosmetic if any code path *reads* it —
  `MemoryStore._mark_superseded` gates an `update_payload` on
  `embedding_status != 'fts5_only'` and would fire a doomed write on the
  now-deleted point. Before assuming a stale field is harmless, grep for its
  *reads*, not just its writes. (Bit us in the source_subsystem purge, #918;
  fixed by #921 Step 2c — reconcile tagged rows to `fts5_only`.)
- **`immutable=1` reads miss WAL-resident writes.** A read-only `sqlite3`
  connection opened with `file:...?immutable=1` reads only the main db file
  and ignores the `-wal`, so a change you JUST committed (still
  un-checkpointed) is *invisible* — you get a false-negative "the write
  didn't land." To verify a live write, use `?mode=ro` (WAL-aware) or query
  through the server/CRUD path; reserve `immutable=1` for historical
  read-only sampling where a little staleness is fine. (A reconcile UPDATE
  read clean under `mode=ro` but appeared unchanged under `immutable=1`.)

### Iterative-Refinement Discipline

AI refinement cycles degrade code they were asked to "improve" — validation
gets stripped, types relaxed, function scope widened. Published measurements
show vague improvement prompts degrade security fastest across iterations.
Three binding rules:

1. **Iterate with scoped, explicit prompts** ("fix the race in X by
   serializing on Y"), never "improve/clean up/make robust".
2. **Be security-explicit when touching validation, auth, or boundaries** —
   state what must not be weakened.
3. **Diff each refinement for what it REMOVED** (constraints, guards, type
   enforcement), not just what it added.

Full failure-mode taxonomy + ordered audit passes: `references/ai-code-audit.md`.

### Anti-Rationalization

These are excuses sessions use to skip discipline. If you catch yourself
thinking any of these, STOP — you are rationalizing a shortcut.

| Rationalization | Why it's wrong |
|---|---|
| "This is just a simple fix, no tests needed" | Simple fixes break complex systems. The Qdrant regression was a "simple fix." Write the test. |
| "I already know what this function does" | You haven't read the implementation. Docstrings lie. Read the actual code. |
| "Tests pass, so we're done" | Tests verify what they cover, not the outcome. Verify actual end-to-end behavior. |
| "I'll clean this up in the next commit" | Next commit never comes in autonomous sessions. Do it now or create a follow-up. |
| "This file is too large to read fully" | Read the relevant section. Partial reads lead to partial understanding and wrong fixes. |
| "The linter is happy, ship it" | Linters catch syntax, not logic. Clean lint with broken behavior is worse than a warning with correct behavior. |
| "This change is low-risk, no impact analysis needed" | Your confidence is based on what you know; checking callers reveals what you don't. Serena `find_referencing_symbols` is live — run it. For multi-hop blast radius, `gitnexus analyze` then `impact`. |
| "I can skip the worktree, I'll be quick" | Concurrent session safety exists because "quick" commits have destroyed work before. Always worktree. |
| "The error is transient, retry will fix it" | Diagnose first. Retrying a misdiagnosed error wastes tokens and masks root causes. |
| "I'll add the follow-up later" | Follow-ups not created in-session are lost. Create it now while context is fresh. |
| "I don't need a skill for this" | If a skill exists, use it. The using-superpowers Red Flags table exists for this exact rationalization. |
| "I can read the summary instead of the source" | Summaries lose context. If you're about to change code, read the code, not the description of it. |

### Code Discovery

Use the right tool for how you're exploring:

- **Architecture overview** — CBM `get_architecture(aspects=["overview"])`
- **Finding symbols** — CBM `search_graph(name_pattern="...")` or Serena `find_symbol`
- **Call tracing** — CBM `trace_path(function_name="...")` or Serena `find_referencing_symbols`
- **Impact / blast radius** — Serena `find_referencing_symbols` (live caller set); GitNexus `impact` (reindex first) for multi-hop + affected processes
- **Config/doc/non-code files** — Grep/Read directly

Full decision matrix: `.claude/docs/code-intelligence.md`

### Auditing Existing Capabilities — enumerate, don't spot-check

Before claiming Genesis "lacks X", "needs to add X", or is "weaker than
<external system> at X" — or before any competitive/architecture comparison —
verify by ENUMERATION, not a spot-check. **Auditing a symbol is not auditing the
stack**, and a negative from a positive search is not evidence of absence:

1. Enumerate the subsystem's full module inventory before concluding anything is absent.
2. Trace the call graph BOTH directions — mechanisms often live in the
   wrapper/caller layer, not the first symbol (CRAG lives in the MCP recall
   wrapper, not `retrieval.py`; the reranker is applied by the caller).
3. Grep by CONCEPT with several synonyms, not one symbol.
4. Verify built/enabled/disabled against RUNTIME state (env gates, server logs),
   not code presence.
5. Multi-path systems → coverage matrix (N entry points × M mechanisms); hot
   auto-fired paths often carry a thinner stack than the deep path — a gradient,
   not an absence.
6. Confidence is capped by enumeration completeness.

A 2026-06-30 competitive audit wrongly claimed Genesis lacked CRAG,
scope-before-rank, and a live reranker — all three had already shipped. Full
protocol: procedure `codebase_audit` / CC memory `audit-enumerate-not-spotcheck`.
For "does Genesis already have X", consult the subsystem map
(`docs/architecture/CURRENT.md`, via the `subsystem-map` skill) FIRST;
`references/codebase-map.md` stays the package-level structural companion.

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

Two protocol steps apply to every review at "Code-reviewer inline" level or
above (full definitions in `.claude/agents/genesis-architect.md`):

- **Scope-drift check first**: compare stated intent (plan file / PR
  description / commit messages) against `git diff --stat` vs the merge-base,
  and open the review with the `Scope Check: CLEAN / DRIFT DETECTED /
  REQUIREMENTS MISSING` + Intent/Delivered block. Informational, never
  blocking.
- **Completion status last**: every review (and every skill workflow that
  concludes work) ends with exactly one of DONE / DONE_WITH_CONCERNS /
  BLOCKED / NEEDS_CONTEXT — with concerns listed, or blocker + what was
  tried, or exactly what context is missing. Findings use the
  BLOCKER / SHOULD-FIX / NOTE severity ladder with per-finding confidence
  and the pre-emit quote gate (a finding must quote its motivating
  file:line or be confidence-capped).

## Pre-Commit Gate

Verify before any commit:

- `git diff --cached --stat` — every file in the diff belongs to your work
- `git status --short` — check untracked files (should be staged or ignored)
- Review level applied matches the adaptive protocol above
- Staged files do not include secrets (`secrets.env`, `.env`, credentials)
- **Private-data scan before every push (public repo).** Grep the ENTIRE diff
  (`git diff origin/main...HEAD`) for private/identifying data — real names,
  company/product names, emails, IPs, private career/project specifics, verbatim
  user messages. Check ALL surfaces, not just prose: **source comments,
  docstrings, and test fixtures/data** are the easy misses. Use a synthetic
  stand-in in tests, never the real private artifact. (2026-07-01: a verbatim
  private DM leaked via a test docstring + a code comment after the commit
  message and PR body were already clean.)
- GROUNDWORK-tagged code not accidentally deleted
- New capabilities registered in `_capabilities.py` + bootstrap manifest
- **Conventional commit prefixes**: `feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`. Scope optional: `feat(ego): add cadence manager`.
  Subject line under 72 characters. Dominant category wins if mixed.
- **NEVER push to main or merge into main without a PR and user approval.**
  Enforced by PreToolUse hook.
- **Targeted tests during development.** Run ONLY the relevant test file(s)
  for your changes. NEVER run the full test suite locally — CI handles that.
  Check CI via `gh pr checks`. Bare `pytest` without a file path is banned.
- **Commit continuously**: after every logical unit of work. Uncommitted = lost.

## Host-Deploy Gate (merged ≠ deployed)

A merged PR that touches host-deployed paths is **NOT done at merge**. The
guardian and the host VM only pick up changes when `scripts/update.sh` runs —
merging and walking away leaves the host running stale code indefinitely
(observed live: a host guardian sat 3 PRs behind for a week because every
session assumed deploy "happens somehow").

**Trigger paths** (match = this gate applies): `src/genesis/guardian/`,
`scripts/guardian-gateway.sh`, `scripts/install_guardian.sh`,
`scripts/host-setup.sh`, `scripts/update.sh`, `scripts/lib/cc_version.sh`.

**After merging such a PR, in the same session:**

1. Run `scripts/update.sh` from `~/genesis` (it redeploys the guardian when
   guardian-relevant paths changed and heals host/container CC + Node pin
   drift — including on a no-delta run).
2. Verify the deploy landed: gateway `version` op reports the expected
   `deployed_commit` / CC version; guardian tick healthy in its journal.
3. State the deploy + verification result explicitly in the wrap-up. If the
   deploy cannot happen this session (host unreachable), create a follow-up
   via `follow_up_create` — never leave deploy as an implicit assumption.

**The reverse direction is equally binding**: host VMs are deploy targets,
never edit-in-place dev environments. An emergency hand-edit on a host gets a
same-day PR that lands the same change at source — a host divergence that
outlives its incident is a bug.

## Pre-Merge Gate

`git_push_guard.py` enforces a **hard gate** on review findings:

1. After CI passes, the merge hook automatically checks PR comments
   for automated review findings (ERROR, [P1], HARD BLOCK).
2. If review present with **blocking findings** → merge is **BLOCKED**
   by the hook (exit code 2). Fix the findings first.
3. If review present with only WARNINGs/NOTEs → merge allowed.
4. If no review comments at all (quota exhausted) → merge allowed
   on CI alone. Note in PR that review was quota-limited.
5. **Override**: Append `# review-override` to the merge command to
   bypass the gate (e.g., `gh pr merge 123 --squash --admin  # review-override`).
   The override is logged. Use only when findings are intentionally accepted.
6. **Read the PR's warning comments before merging — not just the hard gate.**
   Beyond Codex, a structural-review bot posts under the repo-owner account
   (`WingedGuardian`, review state COMMENTED) and emits **SOFT WARNINGs** (PII /
   private-text / wording) that the hook does NOT block on and that a naive
   `.comments` scan misses. Check BOTH `gh pr view N --json reviews,comments`
   and `gh api repos/<owner>/<repo>/pulls/N/comments`, and address each soft
   warning or consciously accept it. Never merge past an unread warning.
7. **Codex findings are INLINE review comments — invisible to `gh pr view`.**
   Codex's review *body* is boilerplate ("Here are some automated review
   suggestions"); its actual `[P1]`/`[P2]` findings live only at
   `gh api repos/<slug>/pulls/N/comments`. Derive `<slug>` live —
   `gh repo view --json nameWithOwner --jq .nameWithOwner` — NEVER hardcode
   it (configs name several repos; the working repo is not the org default).
   A **404 from that endpoint means WRONG SLUG or PR number, never "no
   findings"** — a clean PR returns `[]`. The merge-gate hook only blocks
   ERROR/[P1]/HARD BLOCK, so unread P2s pass silently (2026-07-10: 8 real
   P2s on the entity-layer PRs were merged past this exact way).

## Reference Router

Read references ONLY when relevant to the specific task. Do NOT load all
references on every trigger.

| When you need... | Read... |
|---|---|
| Subsystem purpose/maturity/do-not-touch (judgment layer) | `docs/architecture/CURRENT.md` |
| Codebase structure, package map, gotchas, debugging | `references/codebase-map.md` |
| Package/module/symbol navigation (progressive drill) | `codebase_navigate` MCP tool (L0→L1→L2) |
| venv, DB paths, Qdrant, Ollama, network, commands | `references/environment.md` |
| Worktree rules, concurrent sessions, branch naming | `references/worktrees.md` |
| tracked_task, exc_info, os.killpg, logging patterns | `references/observability.md` |
| V3 state, build order, GROUNDWORK, architecture docs | `references/architecture.md` |
| Phase 6 contribution pipeline, sanitizer | `references/contribution.md` |
| Pending work, active incidents, subsystem status | `references/build-state.md` |
| Auditing/deep-reviewing AI-generated code (failure taxonomy, audit passes) | `references/ai-code-audit.md` |
| Which code tool to use (CBM vs Serena vs GitNexus vs Grep) | `.claude/docs/code-intelligence.md` |

**Freshness rule:** On first read of `codebase-map.md` in a session,
verify structural claims against current code. If a package status or
gotcha has changed, flag to user before acting on stale assumptions.
`docs/architecture/CURRENT.md` carries per-entry `verified:` stamps
enforced by `scripts/check_subsystem_map.py` (CI `subsystem-map-check`) —
after changing a subsystem's capabilities, update its entry and stamp.

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
- **Individual campaigns are user data, not infrastructure** — a campaign's
  name/prompt/targets/cadence live only in the `campaigns` DB table and the
  private backups repo; never hardcode them into tracked source. Unlike modules
  (which ship defaults under `config/modules/*.yaml`), campaigns ship ZERO
  defaults (no `config/campaigns/`). Only campaign infrastructure ships. Express
  reusable session types as generic roles (e.g. the `community-responder`
  profile), not names coupled to a live campaign. See `src/genesis/campaigns/__init__.py`.
- **External egress is gated; owner-facing egress is not** — any autonomous send to the
  outside world (Discord, Medium, Twitter/X, Slack, `DistributionManager.distribute`) MUST
  route through the capability shadow-gate (`autonomy/shadow_gate`) before the enforce stage;
  the `scripts/check_external_io.py` CI guard backstops new endpoints. Delivery TO the owner
  (Telegram/voice/email-to-owner) is NEVER gated. Full contract in `autonomy/shadow_gate.py`.
