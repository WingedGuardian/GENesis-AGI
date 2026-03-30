# Deferred Memory System Work

Created: 2026-03-24
Context: Memory system redesign session (branch fix/proactive-memory-surfacing)
These items were identified during the architectural audit but deferred from the
immediate routing fix. Each is a distinct piece of work.

## 1. Observation Vector Indexing

**Problem:** Observations are SQLite-only. They can't be found via semantic
(vector) search. The `retrieved_count=0` cognitive state flag is partly caused
by this — internal systems can't semantically match observations to relevant
context.

**What it would take:**
- Dual-write observations to both SQLite and Qdrant `episodic_memory`
- Add `scope: "internal"` tag (observations are Genesis's internal processing)
- Update reflection pipeline to use vector search for observation retrieval
- This would help fix the `retrieved_count=0` problem

**Complexity:** Medium. New write path in observation_writer.py, need to handle
embedding failures gracefully (observations must never be lost).

**Blocked by:** Nothing. Can be done anytime after the routing fix lands.

## 2. Automatic Procedure Extraction

**Problem:** The extraction pipeline dumps everything into episodic memory.
Facts that are actually "how to do X" procedures never get routed to
`procedural_memory`. Currently procedures are only created via explicit
`procedure_store` MCP calls.

**What it would take:**
- Add LLM classification step in extraction pipeline
- "Is this extracted fact a procedure (task → steps)?"
- If yes, auto-create procedural_memory entry
- Need confidence thresholds to avoid noisy procedure creation

**Complexity:** Medium-High. Requires prompt engineering for classification,
testing for false positive rate, integration with learning pipeline.

**Blocked by:** Nothing, but lower priority than observation indexing.

## 3. Dead Letter Queue Investigation

**Problem:** 24 dead letters from `chain_exhausted:contingency_foreground` —
all providers exhausted. 3 from `light_reflection`. This is a provider
availability issue, not a memory issue.

**What to investigate:**
- Which providers are in the routing chain?
- Are they rate-limited, down, or misconfigured?
- Is the contingency dispatch system retrying correctly?
- Should there be a fallback behavior when all providers exhaust?

**Complexity:** Low-Medium. Likely a configuration or provider health issue.

## 4. retrieved_count=0 Root Cause

**Problem:** The cognitive state shows `retrieved_count=0` and
`influenced_count=0` across all observations. This means the reflection
pipeline creates observations but never retrieves/uses them.

**Related to:** Item #1 (observation vector indexing) is part of the fix, but
the reflection pipeline's SQL-based observation query also needs investigation.
The pipeline should be querying and using existing observations during
reflection cycles.

**What to investigate:**
- Is the reflection context gatherer actually calling observation queries?
- Are the queries returning results?
- Is the increment_retrieved_batch() being called?
- Trace the full reflection cycle end-to-end

**Complexity:** Medium. Requires tracing the reflection pipeline.

## 5. Memory Link Graph for Knowledge

**Problem:** `memory_links` only connects episodic memories (auto-linked at
store time). Knowledge units and observations are isolated — no graph
traversal, no connectivity scoring.

**What it would take:**
- Enable auto_link for knowledge entries stored via knowledge_ingest
- Consider cross-collection links (episodic ↔ domain)
- Update activation scoring to include knowledge link counts

**Complexity:** Low. Auto-linking already exists, just needs to be enabled
for knowledge entries.

## 6. Option C Upgrade (Three Stores)

**Problem:** Currently `episodic_memory` contains both user-relevant and
internal processing memory, distinguished by `scope` tags. Option C would
physically separate these into `user_memory` and `internal_memory`.

**When:** Only if noise becomes a problem — internal entries polluting
user-facing recall despite scope filtering. The scope tags added in the
routing fix make this migration trivial when needed.

**Complexity:** Medium. Data migration + code changes similar to routing fix.
