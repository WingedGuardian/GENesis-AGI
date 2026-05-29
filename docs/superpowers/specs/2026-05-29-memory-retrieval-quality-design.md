# Design Spec: Memory Retrieval Quality + Graph Wiring + Dream Cycle Enhancement

**Date:** 2026-05-29
**Status:** Draft
**Source:** Inbox review session — GBrain analysis, Iusztin entity resolution
posts, agent product analytics, Honcho observation patterns
**Scope:** Wire existing graph infrastructure into retrieval, extend dream
cycle, add entity resolution, establish retrieval quality metrics

---

## Context

Genesis has substantial graph infrastructure (77K edges, NetworkX, 11 typed
relationships, centrality computation, BFS traversal) that is NOT used in
the retrieval hot path. GBrain's backlink-boosted ranking produces a
+31.4 P@5 improvement — the single largest quality signal in their system —
using the same pattern Genesis could implement with existing data.

Additionally, Genesis's dream cycle runs a single consolidation phase while
GBrain runs 17 phases covering link repair, orphan detection, theme
discovery, and entity enrichment. Genesis detects only 2 contradictions out
of 77K edges, meaning the system is essentially blind to conflicting
information in its own memory.

This spec covers the concrete wiring and enhancement work identified during
the 2026-05-29 inbox evaluation review.

---

## Part 1: Graph-Boosted Retrieval

### 1.1 Backlink Boost

**What:** After RRF fusion produces ranked results, multiply each result's
score by a factor derived from its inbound link count in `memory_links`.

**Formula** (from GBrain, validated at +31.4 P@5):
```python
BACKLINK_BOOST_COEF = 0.05
boosted_score = score * (1 + BACKLINK_BOOST_COEF * math.log(1 + inbound_link_count))
```

This is logarithmic — diminishing returns at high link counts:
- 0 links: 1.00x (no change)
- 5 links: 1.09x
- 10 links: 1.12x
- 50 links: 1.20x
- 100 links: 1.23x

**Where:** `src/genesis/memory/retrieval.py`, after RRF fusion (step 7 in
the current pipeline), before final ranking.

**Implementation:**
1. After collecting all candidate memory_ids from RRF, batch-query inbound
   link counts:
   ```sql
   SELECT target_id, COUNT(*) as link_count
   FROM memory_links
   WHERE target_id IN (?, ?, ...)
   GROUP BY target_id
   ```
2. Apply boost formula to each candidate's fused score.
3. Re-sort by boosted score.

**Estimated effort:** ~50 lines. The SQL query is trivial, the boost is one
line per result.

**Implementation note (Sprint 1):** Also fixed the N+1 per-ID
`count_links()` loop in activation scoring (step 5) by introducing
`batch_link_counts()` which returns both bidirectional and inbound-only
counts in 2 batch queries instead of N individual queries. Inbound counts
are then reused for the backlink boost in step 7b. Chunks at 400 IDs for
SQLite placeholder safety. Link distribution validated: median inbound=3,
99th pct=39, max outlier=1680 (37% boost with log dampening).

### 1.2 Adjacency Boost

**What:** If multiple top-K results link to each other (graph cluster
coherence), give a small boost. This rewards results that form a coherent
knowledge cluster rather than isolated hits.

**Formula:**
```python
ADJACENCY_BOOST = 1.05  # 5% bump
# For each result in top-K:
#   Count how many OTHER top-K results link to it
#   If >= 2, apply boost
```

**Where:** Same location as backlink boost, applied after backlink boost.

**Implementation:**
1. Take top-K results (K=20)
2. For each, check if ≥2 other top-K results have edges to it in
   `memory_links`
3. If yes, multiply score by 1.05

**Estimated effort:** ~30 lines. Uses the same link data already loaded.

### 1.3 Floor-Gated Boost Stacking

**What:** Prevent weak results from accumulating boosts past legitimate hits.
Results below a floor threshold skip ALL boost stages.

**Formula:**
```python
FLOOR_RATIO = 0.85
floor_score = top_score * FLOOR_RATIO
# Skip boosts for any result with pre-boost score < floor_score
```

**Why:** Without floor gating, a low-relevance memory with 100 backlinks
could boost past a high-relevance memory with 0 backlinks. The floor ensures
retrieval relevance always dominates structural signals.

**Where:** Computed once before applying any boosts. Applied as a guard
condition around backlink + adjacency boosts.

**Estimated effort:** ~10 lines.

### 1.4 Centrality Wiring (Deferred)

`centrality_scores()` already exists in `graph.py` but computing betweenness
centrality on 77K edges is expensive (~200-500ms even with k-approximation).
This should NOT be in the retrieval hot path.

**Alternative:** Pre-compute centrality scores periodically (dream cycle or
awareness tick), store in a `memory_centrality` table or as metadata on
`memory_metadata`. Use pre-computed values as an additional boost factor in
retrieval — same pattern as activation scoring.

**Deferred to Part 3** (dream cycle enhancement).

---

## Part 2: Entity Resolution

### 2.1 Surface Form Resolution (Layer 1 — At Ingestion)

**What:** Normalize entity names to canonical forms before storage.

**Implementation:**
1. Maintain an alias dictionary in `~/.genesis/config/entity_aliases.yaml`:
   ```yaml
   aliases:
     "CC": "Claude Code"
     "claude-code": "Claude Code"
     "LLM": "large language model"
     "SK": "SkillOpt"
   ```
2. In `memory_store` and `knowledge_ingest_source`, run content through
   a normalization step that expands known aliases.
3. The dictionary grows over time — dream cycle discovers new aliases.

**Where:** New function in `src/genesis/memory/entity_resolution.py`.
Called from `store.py` before Qdrant upsert.

**Estimated effort:** ~60 lines + config file.

### 2.2 Deduplication Scan (Layer 2 — Periodic)

**What:** Find near-duplicate memories using embedding similarity + graph
structure + content overlap. Apply confidence-tiered merge decisions.

**Implementation:**
1. Query Qdrant for high-similarity pairs (cosine > 0.92) in each collection
2. For each candidate pair:
   a. Check if they share `supports`/`related_to` links (graph signal)
   b. Compare content for semantic overlap (quick LLM check)
   c. Score confidence:
      - ≥0.95: Auto-merge (keep newer, soft-delete older, preserve links)
      - >0.85: Flag as observation for review
      - ≤0.85: Ignore (similar but distinct)
3. Log decisions for audit trail

**Where:** New function in `src/genesis/memory/entity_resolution.py`.
Called from dream cycle (Part 3).

**Estimated effort:** ~150 lines.

### 2.3 Contradiction Detection

**What:** Find memories that contradict each other. Currently 2 out of 77K
links are `contradicts` type — the system is essentially blind.

**Implementation:**
1. During dedup scan, when two memories are about the same topic but contain
   conflicting claims, create a `contradicts` link
2. Contradiction criteria:
   - High embedding similarity (>0.85) — same topic
   - Different or opposing content (LLM classification)
   - Different timestamps — temporal resolution possible
3. For temporal contradictions (e.g., "cost is $200/mo" vs "cost is $231/mo"),
   the newer memory supersedes — mark the older as `succeeded_by`

**Where:** Same module as dedup scan. Integrated into dream cycle.

**Estimated effort:** ~100 lines (the LLM call is the same as dedup
assessment, just with a different decision branch).

---

## Part 3: Dream Cycle Enhancement

### 3.1 Current State

Genesis's dream cycle runs one phase: semantic clustering + consolidation.
GBrain runs 17 phases. Genesis needs to add at minimum:

### 3.2 New Phases

**Implementation note (Sprint 2):** All four phases below are implemented.
Phases run after existing consolidation, each with individual try/except
error isolation. Minimal refactor approach — existing `run()` structure
preserved, new phases appended at the end. Each phase handles `dry_run`
internally.

**Phase 5: Link Repair (after consolidation)**
- Pure SQL: finds memory_ids in `memory_links` that don't exist in `memory_metadata`
- Removes all links involving orphaned IDs
- File: `src/genesis/memory/dream_link_repair.py`

**Phase 6: Entity Resolution Scan**
- Finds near-duplicate memories via Qdrant similarity search (threshold 0.92)
- Auto-merge at ≥0.95 cosine (newer survives, older deprecated)
- LLM check at 0.85-0.95 via `dream_cycle_entity_check` call site (free-tier SLM, 50/run cap)
- Contradiction detection: creates `contradicts` or `succeeded_by` links
- Full audit trail: `entity_resolution_audit` table preserves both memory
  contents, cosine score, LLM verdict, and survivor ID for post-hoc review
- File: `src/genesis/memory/dream_entity_scan.py`

**Phase 7: Orphan Detection**
- Finds memories with zero links in the graph (52% of corpus)
- Searches for similar linked memories (threshold 0.80, max 200/run)
- Creates `related_to` links for discoverable connections
- File: `src/genesis/memory/dream_orphan_detection.py`

**Phase 8: Centrality Recomputation**
- Calls existing `centrality_scores(db, top_n=500)` (k=200 approximation)
- Persists to `centrality_cache` table (atomic DELETE + INSERT replacement)
- Runs even in dry_run (observational data, no destructive effect)
- File: `src/genesis/memory/dream_centrality.py`

**Phase 9: Theme Discovery (Future)**
- Cross-session pattern identification
- Find concepts that appear across many sessions but aren't explicitly linked
- Create `categorized_as` links to discovered themes
- This is the most complex phase — defer to a later sprint

### 3.3 Phase Coordination

- Each phase in its own try/except — one failing doesn't block the next
- Report dict includes per-phase sub-reports with timing and stats
- Existing advisory lock prevents concurrent dream cycles
- Dry_run: consolidation skips writes; new phases handle dry_run internally

### 3.4 Tiered Enrichment by Reference Count

**What:** Entities (people, tools, concepts) referenced frequently across
memories deserve more enrichment than one-off mentions.

**Implementation:**
- Count references per entity across `memory_links` (inbound degree)
- Tier 1 (≥8 references): Full enrichment — web research, entity page,
  comprehensive profile
- Tier 2 (3-7 references): Light enrichment — basic profile, key facts
- Tier 3 (1-2 references): Stub only — name and context of first mention

**Where:** Dream cycle Phase 2 or a separate surplus task.

**Estimated effort:** ~100 lines for the tiering logic. Enrichment actions
use existing web_search/web_fetch infrastructure.

---

## Part 4: Retrieval Quality Metrics

### 4.1 Retrieval Audit Trail

**What:** Log every `memory_recall` query with its parameters and result
summary. This is the prerequisite for any quality improvement loop.

**Implementation decision (Sprint 1):** No new `recall_audit` table needed.
The J-9 eval hooks (`emit_recall_fired` in `src/genesis/eval/j9_hooks.py`)
already capture recall events with query, result_count, top_scores,
memory_ids, latency_ms, source, and intent_category into the generic
`eval_events` table. Sprint 1 extended `emit_recall_fired` with three
additional optional fields:
- `graph_boost_applied: bool` — whether backlink/adjacency boosts fired
- `mean_score: float` — average score across returned results
- `wing: str` — the wing filter if one was applied

This avoids table proliferation while providing all the data needed for
quality analysis. Re-query detection (4.2) and benchmarking (4.3) can
query the existing `eval_events` table filtered by `event_type='recall_fired'`.

### 4.2 Re-Query Detection

**What:** Detect when the user asks the same question differently — a signal
that the first recall failed.

**Implementation:**
- After each recall, compute embedding of the query
- Compare against recent queries (last 10 minutes) in `recall_audit`
- If cosine similarity > 0.85 to a recent query, flag as "re-query" —
  the previous recall probably failed to satisfy
- Store the re-query flag in the audit trail

**Where:** Same as audit trail, with a Qdrant similarity check added.

**Estimated effort:** ~30 lines.

### 4.3 Quality Benchmarking (Future)

**What:** Establish P@5 and R@5 benchmarks for Genesis retrieval.

**Implementation:**
- Build a test set: 50+ query/expected-result pairs from real usage
- Run retrieval against test set periodically (weekly)
- Track precision@5 and recall@5 over time
- Alert on regression

**Deferred:** This requires curating a test set, which is manual work. The
audit trail (4.1) provides the raw data to build the test set from. Do 4.1
first, then build the benchmark from the accumulated data.

---

## Part 5: Visual Content Generation

### 5.1 HTML/CSS + Playwright Screenshots

**What:** Generate branded visual content (header images, social cards, quote
cards, infographics) for the content pipeline using existing dependencies.

**Dependencies already installed:** Playwright 1.58, Pillow, CairoSVG,
Jinja2, Chrome. Zero new dependencies needed.

**Implementation:**
1. Create `src/genesis/modules/content_pipeline/visuals/` package
2. Jinja2 templates for each content type:
   - Medium header (1500x750)
   - LinkedIn post image (1200x627)
   - OG/social card (1200x630)
   - Quote card
   - Comparison table
3. Renderer function:
   ```python
   async def render_visual(template: str, context: dict,
                           width: int, height: int) -> bytes:
       async with async_playwright() as p:
           browser = await p.chromium.launch(headless=True)
           page = await browser.new_page(viewport={"width": width, "height": height})
           html = jinja_env.get_template(template).render(**context)
           await page.set_content(html)
           screenshot = await page.screenshot(type="png")
           await browser.close()
           return screenshot
   ```
4. Integration point in `ScriptEngine`: after drafting text, generate
   visuals alongside

**Estimated effort:** ~200 lines + template files.

---

## Part 6: Voice Channel (Vapi)

### 6.1 Architecture

**What:** Add voice/phone calls as a Genesis communication channel via Vapi.

```
Genesis outreach_send(channel="voice", ...)
    └── VapiAdapter (new)
        ├── Outbound: Vapi REST API + Claude as LLM brain
        ├── Inbound: Vapi webhook → Genesis endpoint → dynamic config
        └── Call events/transcripts → event bus → memory
```

**Why Vapi:** BYO LLM (Genesis uses Claude as the call brain — same memory,
same personality), API-first, $0.05/min + provider costs, startup grant
(90K free minutes).

**Implementation:**
1. `src/genesis/channels/voice/adapter.py` — VapiAdapter alongside
   TelegramAdapter and DashboardAdapter
2. Outbound call function: `POST /call` with recipient + assistant config
3. Inbound webhook handler: return assistant config dynamically
4. Transcript capture: call events → `memory_store` as episodic memories
5. Configuration in `genesis.yaml`:
   ```yaml
   voice:
     enabled: true
     provider: vapi
     api_key: ${VAPI_API_KEY}
     phone_number: "+1XXXXXXXXXX"
     default_assistant:
       model: claude-sonnet-4-6
       system_prompt_source: SOUL.md
   ```

**Estimated effort:** ~300 lines + Vapi account setup.

---

## Part 7: Post-Dispatch Verification (Ego)

### 7.1 Level 1 — Shell Verification

**What:** After ego_executor dispatches a session and it completes, run
machine-verifiable checks before marking the proposal as executed. Currently
the ego trusts self-reported session status — zero-output sessions get
marked "complete" with no verification.

**Source:** Codex/CC/Hermes verification pattern (inbox Genesis-33). The
principle: "if you can't verify it from a shell, it isn't done."

**Implementation:**
1. Before dispatch, define expected outputs in proposal metadata:
   ```python
   expected_outputs = {
       "files": ["/path/to/expected/output.md"],
       "min_size_bytes": 500,
       "required_strings": ["## Summary"],  # optional
   }
   ```
2. After dispatch completes, run verification:
   ```python
   async def verify_dispatch_output(proposal) -> VerificationResult:
       for f in proposal.expected_outputs.get("files", []):
           if not Path(f).exists():
               return VerificationResult(passed=False, reason=f"Missing: {f}")
           size = Path(f).stat().st_size
           if size < proposal.expected_outputs.get("min_size_bytes", 0):
               return VerificationResult(passed=False, reason=f"Too small: {size}B")
       return VerificationResult(passed=True)
   ```
3. If verification fails, mark proposal as `failed` (not `executed`),
   create an observation with the failure reason, and surface in next
   ego cycle.

**Where:**
- `src/genesis/ego/session.py` — add `expected_outputs` to execution brief
- `src/genesis/cc/direct_session.py` — add verification step after session
  completion, before updating proposal status

**Implementation note (Sprint 1):** `expected_outputs` is a dedicated
TEXT column on `ego_proposals` (not inside `execution_plan`, which is
always a plain string like "background CC, ~$0.50"). Added via
`_try_alter` migration. Verification module at
`src/genesis/ego/verification.py`. Wired into
`_record_proposal_outcome()` — runs after session success, before
recording outcome. Failed verification transitions proposal from
'executed' to 'failed' via `mark_proposal_verification_failed()`.

**Estimated effort:** ~100 lines (actual: ~180 lines across module + CRUD + wiring).

### 7.2 Level 2 — Cross-Model Review (Future)

Wire Codex CLI for independent review of dispatch outputs. Genesis already
has the `codex` skill infrastructure. After Level 1 verification passes,
optionally dispatch Codex for an independent quality check on the output.
Different model, different blind spots.

**Deferred to after Level 1 is validated in production.**

---

## Part 8: Content Pipeline Enhancements

### 8.1 Content Repurposing Skill

**What:** Take one piece of content (e.g., a Medium article) and
automatically generate derivative content for other platforms with
appropriate format and tone.

**Transformations:**
- Medium article → LinkedIn post (hook + 3-4 key insights + CTA)
- Medium article → Tweet/thread (punchy, conversational, thread structure)
- Medium article → Newsletter excerpt (personal tone, "here's what I found")

**Implementation:**
1. New skill: `src/genesis/skills/content_repurpose/SKILL.md`
2. Input: source content + target platform
3. Output: platform-adapted version following voice-master conventions
4. Templates per platform defining structure, length, tone shift

**Estimated effort:** ~150 lines (skill file + templates).

### 8.2 Audience Segmentation in Drafting

**What:** Content pipeline maintains audience personas and adapts
tone/depth per platform automatically.

**Implementation:**
- Define personas in config: `technical` (developers, deep detail),
  `business` (decision-makers, outcomes-focused), `general` (accessible)
- Platform → persona mapping: Medium=technical, LinkedIn=business
- ScriptEngine includes persona context in drafting prompt

**Estimated effort:** ~50 lines (config + prompt modification).

---

## Part 9: Skill Evolution — Validation Gate + Routing Fixtures

### 9.1 Validation Gate (from SkillOpt)

**What:** Every proposed skill edit is tested on a validation set BEFORE
being accepted. Rejection is the default — prove improvement or revert.

**Implementation:**
1. Each skill gets a `validation/` directory with 5-15 test tasks
2. New `src/genesis/learning/skills/gate.py`:
   ```python
   def evaluate_gate(candidate_score, current_score, best_score):
       if candidate_score > best_score:
           return GateResult(action="accept_new_best")
       elif candidate_score > current_score:
           return GateResult(action="accept")
       else:
           return GateResult(action="reject")
   ```
3. New `src/genesis/learning/skills/harness.py` — execution harness that
   runs a skill against validation tasks via `direct_session_run`
4. Modify `applicator.py`: insert gate between validation and apply

**Estimated effort:** ~300 lines + validation task definitions.

### 9.2 Rejection Memory (Step Buffer)

**What:** Persist rejected proposals as observations so the refiner doesn't
propose the same ineffective changes next week.

**Implementation:**
- On rejection, write observation:
  `type="skill_evolution_rejected"`, content=proposal summary + rejection reason
- Feed recent rejected observations into the refiner prompt as context

**Estimated effort:** ~30 lines.

### 9.3 Routing-Eval Fixtures (from GBrain)

**What:** Each skill ships with test cases that verify it triggers for the
right queries. CI-verifiable skill routing.

**Implementation:**
- `routing-eval.jsonl` per skill: `{"query": "...", "should_trigger": true}`
- Test runner checks that the skill injection hook correctly matches/doesn't
  match each query

**Estimated effort:** ~50 lines + fixture files.

---

## Part 10: Distribution & Surplus Routing

### 10.1 Public MCP Server Spec (Marketing/Distribution)

**What:** Define a public-facing Genesis MCP server spec that any Claude Code
or Cursor session could connect to. Turns Genesis from "install and run" to
"connect and use."

**Capability tiers:**
- Read-only (low trust): `web_search`, `web_fetch`, `knowledge_recall`
- Memory (medium trust): `memory_store`, `memory_recall`
- Outreach (high trust, gated): `outreach_send`, `outreach_queue`
- Autonomy (highest trust): `task_submit`

**Deferred:** This is a marketing/distribution decision, not a development
one. Track for the GTM campaign. No implementation needed yet — Genesis
already has the MCP infrastructure internally.

### 10.2 Surplus Free-Tier Routing

**What:** Add Mistral's free tier and/or Cloudflare Workers AI to surplus
task routing. Genesis runs ~60 surplus tasks/day — routing appropriate ones
through free-tier models reduces cost.

**Investigation needed:**
- Mistral free tier: ~1B tokens/month, quality sufficient for surplus
  summarization and analysis
- Cloudflare Workers AI: ~20M tokens/month
- Per-provider budget tracking (pattern from FreeLLMAPI) to stay within
  free-tier limits

**Implementation:** Add providers to `model_routing.yaml` with surplus-only
routing rules. Add token budget tracking per provider per day.

**Estimated effort:** ~100 lines (routing config + budget tracker).

---

## Sequencing

### Sprint 1: Graph-Boosted Retrieval + Post-Dispatch Verification (2-3 days)
- 1.1 Backlink boost (~50 lines)
- 1.2 Adjacency boost (~30 lines)
- 1.3 Floor-gated boost stacking (~10 lines)
- 4.1 Retrieval audit trail (~40 lines)
- 7.1 Post-dispatch shell verification (~100 lines)
- Test: verify retrieval quality improves on sample queries; verify
  dispatch verification catches zero-output sessions

### Sprint 2: Dream Cycle Enhancement + Entity Resolution (3-4 days)
- 3.2 Phase 0: Link repair
- 3.2 Phase 2: Entity resolution scan (2.2 + 2.3)
- 3.2 Phase 3: Orphan detection
- 3.2 Phase 4: Centrality recomputation
- 2.1 Surface form resolution at ingestion
- Test: run enhanced dream cycle, verify new phases complete

### Sprint 3: Skill Evolution Quality (2-3 days)
- 9.1 Validation gate + execution harness
- 9.2 Rejection memory (step buffer)
- 9.3 Routing-eval fixtures for top 5 skills
- Test: propose a skill change, verify gate accepts/rejects correctly

### Sprint 4: Content Pipeline (3-4 days)
- 5.1 Visual templates + Playwright renderer
- 8.1 Content repurposing skill
- 8.2 Audience segmentation
- Integration with ScriptEngine
- Test: generate Medium article with header image + LinkedIn derivative

### Sprint 5: Voice Channel (2-3 days)
- 6.1 VapiAdapter
- Outbound/inbound call handling
- Transcript capture
- Test: make a test call with Genesis brain

### Sprint 6: Surplus Optimization (1-2 days)
- 10.2 Add Mistral free tier + Cloudflare Workers AI to surplus routing
- Per-provider token budget tracking
- Test: verify surplus tasks route to free tier and stay within limits

### Parallel workstreams:
- Honcho integration (separate spec: 2026-05-29-honcho-integration-db-adapter-design.md)
- DB adapter + Postgres groundwork (same spec as Honcho)
- Retrieval quality metrics (4.2 re-query detection, 4.3 benchmarking)
- Public MCP server spec (10.1 — marketing/distribution, no code needed yet)

---

## Success Criteria

- **Retrieval:** Backlink boost measurably improves result quality on sample
  queries (A/B comparison with audit trail data)
- **Post-dispatch:** Shell verification catches at least one zero-output
  dispatch session that would have been marked "complete" without it
- **Dream cycle:** All new phases complete without error; contradiction
  detection finds >0 contradictions; orphan detection identifies disconnected
  memories
- **Entity resolution:** Dedup scan quantifies duplication rate; auto-merges
  at ≥0.95 confidence work correctly
- **Skill evolution:** Validation gate correctly rejects at least one
  proposal that would have auto-applied under the old system
- **Content pipeline:** Produces Medium article with branded header image +
  auto-generated LinkedIn post derivative
- **Voice:** Outbound test call completes with Genesis as conversation brain
- **Surplus:** Free-tier routing reduces surplus task cost by ≥30%
- **Audit:** 30 days of retrieval audit data accumulated for quality analysis
