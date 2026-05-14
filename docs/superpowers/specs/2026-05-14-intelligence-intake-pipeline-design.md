# Intelligence Intake Pipeline — Design Spec

**Date:** 2026-05-14
**Status:** Draft
**Scope:** Surplus insight routing, recon pipeline redesign, Deep reflection changes, code intelligence hook, web search capture

## Context

Genesis has two intelligence-gathering systems — surplus (anticipatory research, brainstorms, code audits) and recon (GitHub monitoring, email scanning, model intelligence) — that both produce findings but route them through disconnected, broken, or nonexistent pipelines.

**Current problems:**
- Surplus insights stage in `surplus_insights` table waiting for Deep reflection to promote/discard. Deep runs every 48h and reviews max 20 items. Backlog: 206 pending, 2.7% lifetime promotion rate.
- Multi-finding surplus outputs (e.g., 3 separate topics from anticipatory research) are stored as single blobs. If promoted, they become one low-quality observation mixing unrelated topics.
- Recon has 6 scheduled jobs but only 3 have implementations. `web_monitoring` and `source_discovery` are ghost schedules with no code. `model_intelligence` has code but is not wired to fire.
- `github_landscape` is a firehose — 204 raw release note dumps with no quality filter.
- `email_recon` works well but output sits in observations without reaching the knowledge base.
- Web search results from foreground/background sessions evaporate after the session ends. No systematic capture.
- The code intelligence PreToolUse hook fires once per session then goes silent, providing no ongoing guidance.

**Design principle:** Triage is an intake function (fast, cheap, immediate), not a cognitive function (slow, expensive, batched). Don't re-triage what's already been triaged.

## Architecture

```
Surplus tasks ──┐
                │
Recon jobs ─────┤──→ [ Atomize ] ──→ [ Score (conditional) ] ──→ [ Route ]
                │     (always,        (only for uncurated         ├─→ Knowledge base
Web search ─────┘      programmatic)   sources; LLM for recon,    ├─→ Observation
                                       skip for surplus/sessions)  └─→ Discard
```

Input pipelines maintain their own scheduling and query generation. They converge at a shared intake module that handles atomization, scoring, and routing.

## Item D: Code Intelligence Hook Fix

**File:** `~/.claude/hooks/cbm-code-discovery-gate`

**Changes:**
- Counter-based: fires advisory reminder every 3rd qualifying call (not every call, not just once)
- Path-aware: only counts calls targeting `src/genesis/` or `.py` files under the project root. Calls to config, docs, YAML, markdown — no counter increment, no reminder
- Reads tool input JSON from stdin to extract target path
- Counter resets per session (PPID-based gate file, same mechanism as current)
- Always exit 0 (never blocks)

## Item A: Atomization

**New function:** `atomize(content: str, source_task_type: str) -> list[AtomicFinding]`

**Location:** `src/genesis/surplus/intake.py`

**Approach:** Control format at the source. For task types that produce multi-finding outputs (anticipatory_research, code_audit, gap_clustering), modify surplus task prompts to require structured JSON output:

```json
{
  "findings": [
    {
      "title": "...",
      "content": "...",
      "sources": ["https://..."],
      "relevance": "Why this matters to Genesis"
    }
  ]
}
```

Atomization then iterates the array — no regex, no markdown parsing.

**Fallback chain:**
1. Parse as JSON with `findings` array — ideal path
2. JSON but no `findings` key — treat whole output as single item
3. Not valid JSON — attempt markdown split (best effort: numbered items, heading-delimited sections)
4. Markdown split fails or uncertain — treat as single item, don't force a bad split

**Observability:** Log which fallback path was taken per intake. Track structured output success rate by model.

Task types that produce single narrative outputs (brainstorm_self, self_unblock) skip atomization — flow through as single items.

## Item C: Intake Pipeline

**New module:** `src/genesis/surplus/intake.py`

Three functions called in sequence:

```python
atomize(content, source_task_type) -> list[AtomicFinding]
score(finding, source_type) -> ScoredFinding    # conditional
route(scored_finding) -> "knowledge" | "observation" | "discard"
```

### Scoring — conditional by source

Sources with existing LLM curation skip the scoring step and inherit confidence from their source tier:

| Source | Skip scoring? | Confidence | Rationale |
|--------|--------------|------------|-----------|
| User-directed ingestion | Yes | 0.9 | Explicit "store this" |
| Foreground web research | Yes | 0.75 | User-directed, specific |
| Background task research | Yes | 0.7 | Explicit Sonnet call for research |
| Anticipatory research (surplus) | Yes | 0.6 | LLM-curated, context-aware |
| model_intelligence recon | TBD | 0.6 | Structured API data, check if LLM-curated |
| email_recon | Yes | 0.65 | Two-layer Gemini + CC triage |
| github_landscape | LLM score | 0.5 | Mechanical scrape, no quality filter |
| web_monitoring | LLM score | 0.5 | Automated URL change detection |
| free_model_inventory | LLM score | 0.5 | Mechanical API scan |
| source_discovery | LLM score | 0.4 | Speculative, needs approval |

### Routing thresholds

- Score >= 0.5 → knowledge base via `knowledge_ingest`
- Score >= 0.7 AND pattern-level insight → also create observation
- Score 0.4-0.5 → store to knowledge at low confidence, no observation
- Score < 0.4 → discard silently

### Call site

`45_intelligence_intake` in the router. Free-tier, Haiku-class. Only invoked for sources that need LLM scoring.

### Integration points

- **Surplus executor:** After task completion, before current `surplus_insights` write. For sources that skip scoring, findings go straight to knowledge. No staging table needed.
- **Recon pipeline:** After findings generation, replaces direct observation storage with intake routing.

### Fallback

If the intake LLM call fails (free-tier unavailable), write to `surplus_insights` as pending with current behavior. Deep can still pick it up. Graceful degradation.

### Observability

Log each intake decision to events: source, atomization path, score, route destination, model used. This is how we verify the pipeline is working.

## Item B: Deep Reflection Changes

### Remove surplus review

- Remove `SURPLUS_REVIEW` from `DeepReflectionJob` enum
- Remove `surplus_decisions` output section from `REFLECTION_DEEP.md`
- Remove `_route_surplus_decision()` from `output_router.py`
- Remove `SurplusDecision` dataclass from `types.py`

### Replace with intelligence digest

Context gatherer replaces `surplus.list_pending(db, limit=20)` with a summary query:
- Count of items triaged since last Deep cycle
- Breakdown by source type
- Top themes/topics

Deep can reference this for pattern recognition, contradiction detection, and strategic thinking — but does not make individual promote/discard decisions.

### Reduce cadence

Update `depth_thresholds` table:
- `floor_seconds`: 172800 (48h) → **86400 (24h)**
- `ceiling_window_seconds`: 86400 (24h) → **86400 (24h)** (unchanged)

Deep runs daily instead of every two days. With surplus review removed, cycles are lighter and faster.

## Recon Pipeline Redesign

### 1. github_landscape — Tone down noise, add intake routing

**Pre-LLM noise reduction:**
- Skip pre-releases: filter on GitHub API `prerelease` boolean field. OpenClaw betas, Cognee dev releases — all gone.
- Reduce `_RELEASES_PER_PROJECT` from 5 to 2.
- Timestamp gate: only process releases published after last successful gather. No re-checking old releases.

**Post-filter:** Remaining releases route through intake pipeline. Confidence 0.5 (automated scan, no prior LLM curation).

### 2. model_intelligence — Wire to scheduler

**Problem:** `ModelIntelligenceJob` class exists with good code but is not dispatched by the scheduler. Only callable via MCP tool.

**Fix:** Add a dedicated scheduler job (like `run_recon_gather`) that instantiates and runs `ModelIntelligenceJob` on the Sunday 6am cron schedule. Wire profile_registry and surplus_queue dependencies.

**Output:** Through intake pipeline. Confidence 0.6 (structured API data, directly actionable for router).

### 3. free_model_inventory — Debug and wire

**Problem:** Cache file shows 0 free models despite running May 12. Either OpenRouter API response format changed or `pricing.prompt == 0` check is failing on string vs float comparison.

**Fix:**
1. Debug the OpenRouter API response — check actual `pricing.prompt` field type
2. Fix the parsing if needed
3. Wire as dedicated daily job (currently only runs as part of model_intelligence)

**Output:** New free models create follow-ups for MODEL_EVAL surplus tasks (logic exists). Through intake pipeline, confidence 0.5.

### 4. email_recon — Route through intake

**Status:** Working well. Quality is excellent (two-layer Gemini + CC triage).

**Change:** Route output through intake pipeline instead of directly to observations. Confidence 0.65 (LLM-curated, authoritative newsletter sources).

No other changes needed.

### 5. web_monitoring — Build infrastructure (empty source list)

**Purpose:** Monitor non-GitHub web sources for content changes. Blog posts, documentation pages, API changelogs, competitor sites.

**Implementation:**
- URL watcher: fetch URL → compute content hash → compare against last fetch → if changed, extract key changes
- Change extraction via intake pipeline LLM scoring step
- Source list managed via `recon_sources` MCP tool or dashboard
- Ships with empty source list — user adds sources when ready

**Cadence:** Weekly (Fridays), per existing schedule.
**Confidence:** 0.5 (automated scan, LLM-summarized changes)

### 6. source_discovery — Build infrastructure (disabled by default)

**Purpose:** Expand the watchlist. Find new repos, tools, blogs relevant to Genesis's domain.

**Implementation:**
- LLM-driven (Haiku via intake call site): prompt with current watchlist + recent cognitive state
- Web search to verify suggestions exist and are active
- Output: proposed new sources as findings, surfaced for user/ego approval
- Never auto-adds to watchlist

**Cadence:** Monthly (1st), per existing schedule. Ships disabled or with first run deferred.
**Confidence:** 0.4 (speculative, requires approval)

## Web Search Capture

**New utility:** `intake.capture_web_result(url, content_summary, query_context, session_type)`

Sessions (foreground, background, surplus) call this when a web search/fetch returns useful results. Not automatic for every fetch — the session decides what's worth capturing based on relevance to the query.

**Confidence by session type:**
- Foreground: 0.75
- Background task: 0.7
- Surplus: 0.6

**Implementation:** Lightweight function that calls into the intake pipeline. Modified session prompts instruct: "For significant web findings, call `capture_web_result()` with URL, key facts, and relevance."

**Contradictory findings:** Both sides stored with source attribution. The consuming session resolves in context. "Source A says X, Source B says Y" is more honest than a triage layer picking a winner.

## Files to Modify

### New files
- `src/genesis/surplus/intake.py` — atomize, score, route functions + AtomicFinding/ScoredFinding types

### Modified files
- `~/.claude/hooks/cbm-code-discovery-gate` — counter-based, path-aware hook
- `src/genesis/surplus/executor.py` — call intake after task completion instead of writing to surplus_insights
- `src/genesis/recon/gatherer.py` — skip pre-releases, reduce releases per project, timestamp gate, route through intake
- `src/genesis/recon/model_intelligence.py` — debug free model parsing
- `src/genesis/surplus/scheduler.py` — add model_intelligence and free_model_inventory as dedicated jobs
- `src/genesis/reflection/context_gatherer.py` — replace surplus.list_pending with intelligence digest
- `src/genesis/identity/REFLECTION_DEEP.md` — remove surplus_decisions section
- `src/genesis/reflection/output_router.py` — remove _route_surplus_decision
- `src/genesis/reflection/types.py` — remove SURPLUS_REVIEW job, SurplusDecision dataclass
- `src/genesis/mail/monitor.py` — route email_recon findings through intake
- `config/model_routing.yaml` — add call site 45_intelligence_intake
- Surplus task prompts — add structured JSON output requirement for multi-finding task types
- `depth_thresholds` table — update Deep floor_seconds to 86400

### New infrastructure (empty, built but not populated)
- Web monitoring URL watcher in recon module
- Source discovery prompt and search mechanism in recon module

## Verification

1. **Hook:** Grep inside `src/genesis/` 3+ times, verify reminder fires on 3rd call but not on config/doc reads
2. **Atomization:** Run anticipatory_research surplus task, verify output splits into separate findings
3. **Intake routing:** Verify findings reach knowledge base with correct confidence, not surplus_insights staging
4. **github_landscape:** Verify pre-releases are filtered, remaining releases route through intake
5. **model_intelligence:** Trigger via MCP tool, verify findings stored with correct category
6. **free_model_inventory:** Debug OpenRouter response, verify free models detected and follow-ups created
7. **Deep reflection:** Trigger a cycle, verify no surplus_decisions in output, verify intelligence digest in context
8. **Deep cadence:** Verify floor_seconds is 86400 in depth_thresholds table
9. **Web capture:** In a foreground session, do a web search, call capture_web_result, verify it reaches knowledge base
10. **Fallback:** Kill free-tier provider, verify surplus insight falls back to staging table for Deep pickup
