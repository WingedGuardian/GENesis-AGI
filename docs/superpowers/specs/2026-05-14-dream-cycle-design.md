# Dream Cycle — Retroactive Memory Consolidation Design

**Date:** 2026-05-14
**Status:** Draft
**Scope:** Episodic memory consolidation, weak link pruning, dead letter cleanup,
transcript archival, entity enrichment (22_tagging)
**Prerequisite:** PR #352 (memory lifecycle bug fixes) merged

## Context

Genesis episodic memory grows monotonically. 19K+ memories, ~734/day. The
extraction pipeline uses exact-content dedup only (FTS5 length + substring match)
with no semantic similarity check before storing. The LLM's quality filter is the
only gate, so near-duplicates accumulate. Nothing consolidates, prunes, or
synthesizes after ingest.

The brain analogy: episodic memories consolidate during sleep. Genesis does the
same on a cron schedule — a "dream cycle."

**Design principles:**
- No time-based confidence decay. Confidence changes on evidence only
  (contradictions, corrections, corroboration). Not because time passed.
- Episodic only. Knowledge base excluded (low volume, upsert handles dedup).
- Observations already healthy (TTL system + daily 2AM sweep). Leave alone.
- Deep reflection is NOT for consolidation. Reflection = strategic thinking.
  Dream cycle = separate infrastructure.
- `deprecated` supplements `invalid_at`. They are independent dimensions:
  `invalid_at` = "fact no longer true in the world." `deprecated` = "memory
  consolidated into a synthesis." A memory must pass both filters to surface.

## Three-Tier Architecture

```
Tier 1 — Mechanical          Tier 2 — 22_tagging           Tier 3 — Dream Cycle
(schedule_maintenance,        (free-tier LLM,               (LLM synthesis,
 no LLM, every 6h)            entity enrichment,             cluster-merge,
                               weekly, up to 200)             weekly Sunday 3am)
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────────┐
│ Weak link pruning   │    │ Entity extraction    │    │ Group by wing/room      │
│ Dead letter cleanup  │    │ (people, projects,   │    │ Cluster by cosine >=0.87│
│ Transcript archival  │    │  tech, decisions)    │    │ Union-find components   │
│ (no LLM needed)     │    │ Feed typed links     │    │ LLM synthesis per clust │
│                     │    │ to NetworkX graph    │    │ Soft-delete originals   │
└─────────────────────┘    └─────────────────────┘    └─────────────────────────┘
      ↓ ships first              ↓ ships last               ↓ ships second
```

**Build order:** Tier 1 first (mechanical, zero risk), then Tier 3 (dream cycle
synthesis — the core value), then Tier 2 (22_tagging enrichment — lower priority,
builds on Tier 3 infrastructure).

---

## Tier 1: Mechanical Maintenance

Runs inside `schedule_maintenance()` in `src/genesis/surplus/scheduler.py`.
No LLM. Direct DB operations. Each wrapped in its own try/except (fixing the
sequential-failure gap identified in PR #352 review).

### 1A. Weak Link Pruning

**Criteria:** `strength < 0.3 AND created_at < (now - 30d)`

Weak links in the NetworkX graph that have never been reinforced. These are
noise from auto-linking that didn't prove useful.

**Implementation:**
```python
# In schedule_maintenance(), after existing GC calls
from genesis.db.crud import memory_links
pruned = await memory_links.prune_weak(
    rt.db, max_strength=0.3, min_age_days=30,
)
```

**New function:** `memory_links.prune_weak(db, max_strength, min_age_days) -> int`

**File:** `src/genesis/db/crud/memory_links.py`

**Safety:** Only prunes links, never memories. NetworkX cache invalidated after
prune via existing `_invalidate_graph_cache()`.

### 1B. Dead Letter Cleanup

**Target:** `chain_exhausted:3_micro_reflection` entries older than 72h.

These are 59% of the dead letter queue — expected noise from 5-minute awareness
ticks hitting free provider rate limits. The existing 72h auto-expire handles
generic entries, but categorized bulk cleanup is cleaner.

**Implementation:** Already handled by existing `purge_expired()` in dead letter
module. Verify the 72h TTL is functioning and add monitoring if not.

### 1C. Transcript Archival

**Target:** `.jsonl` files in `~/.genesis/background-sessions/` older than 90 days.

**Action:** gzip in-place (`.jsonl` -> `.jsonl.gz`). Not deletion.

**Implementation:**
```python
# In schedule_maintenance()
from genesis.surplus.maintenance import archive_old_transcripts
archived = await archive_old_transcripts(
    base_dir=Path.home() / ".genesis" / "background-sessions",
    older_than_days=90,
)
```

**New function:** `archive_old_transcripts(base_dir, older_than_days) -> int`

**File:** `src/genesis/surplus/maintenance.py`

**Safety:** gzip is lossless and reversible. Original `.jsonl` deleted only after
successful gzip write + size verification.

---

## Tier 3: Dream Cycle Synthesis

The core consolidation engine. Finds clusters of semantically near-duplicate
episodic memories, synthesizes each cluster into a single canonical memory via
LLM, and soft-deletes the originals.

### Algorithm

#### Phase 1 — Scroll and Group

Pull all episodic_memory point IDs + payloads from Qdrant via `scroll()`.
Group into `(wing, room)` buckets. This constrains pairwise comparisons to
semantically coherent domains and prevents cross-wing false-positive clusters.

```python
# Batched scroll, 1000 points/page
points = await qdrant_scroll_all("episodic_memory")
buckets = defaultdict(list)
for point in points:
    wing = point.payload.get("wing", "general")
    room = point.payload.get("room", "uncategorized")
    buckets[(wing, room)].append(point)
```

**Skip deprecated memories:** Filter `deprecated == True` from scroll results
before clustering (don't re-cluster already-merged memories).

#### Phase 2 — Cluster Within Each Bucket

For each `(wing, room)` bucket:

1. For each point, call `qdrant.search(vector=point.vector, limit=20,
   score_threshold=0.87)` restricted to the same wing/room via filter.
2. Build a graph: nodes = memory IDs, edges = pairs above threshold.
3. Extract connected components via union-find. Each component with >= 2
   nodes is a candidate cluster.

**Similarity threshold:** 0.87 (configurable)
- Below the potential 0.90 ingest-time near-dup check (catches things that
  would slip through)
- High enough to avoid merging conceptually distinct memories that share
  domain vocabulary

**Performance:** At ~19K points grouped by wing/room, buckets are dozens to
hundreds of points. Pairwise search is fine. Revisit if any bucket exceeds
~2K points (log a warning).

#### Phase 3 — LLM Synthesis Per Cluster

For each cluster (capped at 100 clusters per run):

1. Pull full content of all memories in the cluster.
2. Send to LLM via router chain `dream_cycle_synthesis`.
3. Receive: synthesized content, merged tags, confidence, recommended
   wing/room, synthesis notes.
4. Store via `MemoryStore.store()` with:
   - `source="dream_cycle"`
   - `source_pipeline="dream_cycle"`
   - `confidence=max(original confidences)`
   - `tags` including `"synthesized"` + merged originals' tags
   - `wing`/`room` as returned by LLM (may reclassify)

**Why not `memory_synthesize` MCP tool?** It stores with fixed confidence 0.8
and `source_pipeline="synthesis"`. Dream cycle needs `source_pipeline="dream_cycle"`
for provenance tracking and `dream_cycle_run_id` for rollback. Using
`MemoryStore.store()` directly gives full control. The linking step from
`memory_synthesize` (creates `extends` typed links) should be extracted into a
reusable helper and called with link type `consolidated_from`.

#### Phase 4 — Soft-Delete Originals

For each original in the merged cluster:

1. **Qdrant:** `set_payload()` — add `deprecated: True` and
   `synthesized_into: <new_memory_id>`.
2. **SQLite:** `UPDATE memory_metadata SET deprecated = 1,
   dream_cycle_run_id = ? WHERE memory_id = ?`.
3. **Do NOT hard-delete.** Keep for audit trail and rollback.

**On the new synthesized memory's Qdrant payload:**
- `synthesized_from: list[str]` — IDs of all original memories in the cluster

### Synthesis Prompt

```
You are synthesizing a cluster of related memories into a single canonical record.

These memories are all tagged wing={wing}, room={room}. They share high semantic
similarity (cosine >= 0.87). Your job is to produce ONE memory that is strictly
more informative than any individual original, preserving all unique facts while
eliminating redundancy.

Input memories ({n} total):
{for each: "--- Memory {i} (confidence {c}, source {source}, created {date}) ---\n{content}\n"}

Output JSON:
{
  "content": "<synthesized content — complete, self-contained, no references to 'the above'>",
  "tags": ["<merged relevant tags — deduplicated>"],
  "confidence": <float 0-1, max of inputs as baseline>,
  "memory_class": "<fact|reference|procedure|insight>",
  "wing": "<wing — may differ from inputs if reclassification is warranted>",
  "room": "<room>",
  "synthesis_notes": "<why these were merged, what was dropped>"
}

Rules:
- Never invent facts not present in the inputs
- Preserve all unique details — err on the side of keeping too much
- If memories contradict, note the contradiction explicitly in content
- If memories represent temporal evolution (X was true, then Y), preserve the timeline
- If one memory has much higher confidence, it likely supersedes the others — note this
```

### Routing Chain

**New chain in `config/model_routing.yaml`:**
```yaml
dream_cycle_synthesis:
  chain:
  - groq-free
  - mistral-small-free
  - openrouter-free
  retry_profile: background
```

Free-tier only. The 22_tagging chain is reserved for entity extraction (different
prompt, different purpose). Dream cycle synthesis needs its own chain.

**Call site metadata in `_call_site_meta.py`:**
```python
"dream_cycle_synthesis": {
    "description": "Dream cycle cluster-merge synthesis. Consolidates near-duplicate episodic memories into canonical records.",
    "category": "consolidation",
    "frequency": "Weekly batch (Sunday 3am)",
    "model_tier": "slm",
    "wired": True,
    "status_reason": "ACTIVE",
},
```

### Clusters > 10 Memories

Flag for manual review instead of auto-merging. Write an observation with the
cluster details but don't synthesize. This prevents catastrophic loss from
merging a large, potentially heterogeneous cluster.

```python
if len(cluster) > MAX_CLUSTER_SIZE:  # default 10
    await observation_write(
        source="dream_cycle",
        type="large_cluster_detected",
        content=f"Cluster of {len(cluster)} memories in {wing}/{room} — skipped auto-merge",
        priority="medium",
    )
    continue
```

### Scheduling

**CronTrigger:** `day_of_week="sun", hour=3` — Sunday 3am weekly.

Register in surplus scheduler `start()` method, following the `model_intelligence`
pattern at `src/genesis/surplus/scheduler.py:230-239`:

```python
from apscheduler.triggers.cron import CronTrigger
self._scheduler.add_job(
    self.run_dream_cycle,
    CronTrigger(day_of_week="sun", hour=3),
    id="dream_cycle",
    max_instances=1,
    misfire_grace_time=3600,
)
```

### Run Cap

Max 100 cluster merges per run to bound LLM cost. If the queue exceeds the cap,
prioritize clusters with the most originals (highest redundancy reduction per
synthesis call).

**Cost estimate:** At 19K points with ~5% mergeable pairs: ~950 clusters.
Capped at 100/run. At free-tier models, cost is $0/run. Even paid fallback
would be well under $1/run.

---

## Schema Changes

### SQLite — Migration 0018

**File:** `src/genesis/db/migrations/0018_dream_cycle.py`

```python
async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(memory_metadata)")
    cols = {row[1] for row in await cursor.fetchall()}

    if "deprecated" not in cols:
        await db.execute(
            "ALTER TABLE memory_metadata "
            "ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0"
        )
    if "dream_cycle_run_id" not in cols:
        await db.execute(
            "ALTER TABLE memory_metadata "
            "ADD COLUMN dream_cycle_run_id TEXT"
        )

    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_meta_deprecated "
        "ON memory_metadata(deprecated)"
    )

    # Bonus: index on knowledge_units.qdrant_id (deferred from PR #352)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='knowledge_units'"
    )
    if await cursor.fetchone():
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_units_qdrant_id "
            "ON knowledge_units(qdrant_id)"
        )
```

**Also update schema definition in `src/genesis/db/schema/_tables.py`** —
add `deprecated` and `dream_cycle_run_id` columns to `memory_metadata` CREATE
TABLE statement (after `source_subsystem`).

### Qdrant (schemaless — no migration)

On original memories after merge:
- `deprecated: true` — payload field via `set_payload()`
- `synthesized_into: str` — ID of the new synthesis

On new synthesized memory:
- `synthesized_from: list[str]` — IDs of originals

### Retrieval Filter Changes

**Qdrant filter:** `src/genesis/qdrant/collections.py:117-123`

Add to `must_not_conditions` list (always-on, like subsystem exclusion):
```python
# Always exclude deprecated memories (dream cycle soft-delete)
must_not_conditions.append(
    FieldCondition(key="deprecated", match=MatchValue(value=True))
)
```

**FTS5 filter:** `src/genesis/db/crud/memory.py:196`

Add after the bitemporal `invalid_at` filter:
```python
# Dream cycle deprecation filter
sql += (
    " AND (memory_metadata.deprecated IS NULL "
    "OR memory_metadata.deprecated = 0)"
)
```

---

## Safety

### Dry-Run Mode (Default)

`dry_run: bool = True` parameter on the dream cycle runner. In dry-run:
- Compute clusters and log them
- Write an observation with the cluster report (what would have been merged,
  how many clusters, size distribution)
- Do NOT write any new memories or deprecate anything

**Ship dry-run first.** Run manually, review the cluster report, then enable
live mode via config toggle.

### Rollback

Every dream cycle run generates a UUID `run_id`. Each deprecated original gets
`dream_cycle_run_id = run_id`. Each synthesized memory gets tagged
`dream_cycle_run_id:{run_id}`.

Rollback function:
```python
async def rollback(run_id: str) -> dict:
    """Reverse a dream cycle run.

    1. Find all memories deprecated by this run_id
    2. Clear their deprecated flag
    3. Find the synthesized memories created by this run
    4. Hard-delete the syntheses (they're derived, not original data)
    """
```

### What NOT to Merge

- **knowledge_base collection** — excluded entirely (different collection,
  authoritative references)
- **Contradictory memories** — merge with explicit contradiction note in content;
  do not pick a winner
- **Temporal sequences** — if memories document evolving state over time, preserve
  the timeline rather than collapsing to latest
- **Clusters > 10** — flag for review, don't auto-merge
- **Already-deprecated memories** — skip during scroll phase

---

## Tier 2: 22_tagging Entity Enrichment (Lower Priority)

**Ships after Tier 1 + Tier 3.** Depends on dream cycle infrastructure being
proven.

### Purpose

Batch-enrich memories lacking entity tags. Extract structured entities (people,
projects, technologies, decisions) and feed typed links into the NetworkX graph.

### Approach

- Bring the `22_tagging` call site online (currently V4_PLACEHOLDER, wired=False)
- Use existing chain: `mistral-small-free → groq-free → openrouter-free`
- Weekly schedule, up to 200 memories per run
- Target: memories with no entity-type tags (no `entity:*` in tags list)
- Output: entity tags added to Qdrant payload + typed links created

### Design Deferred

Full design for Tier 2 deferred to implementation time. The 22_tagging chain
and call site infrastructure are ready. The entity extraction prompt and link
type mapping need design work.

---

## Files to Create/Modify

| File | Action | Tier |
|------|--------|------|
| `src/genesis/memory/dream_cycle.py` | Create — main consolidation logic | 3 |
| `src/genesis/db/migrations/0018_dream_cycle.py` | Create — schema migration | 3 |
| `src/genesis/db/crud/memory_links.py` | Modify — add `prune_weak()` | 1 |
| `src/genesis/surplus/maintenance.py` | Modify — add `archive_old_transcripts()` | 1 |
| `src/genesis/surplus/scheduler.py` | Modify — register CronTrigger, add maintenance calls | 1+3 |
| `src/genesis/qdrant/collections.py` | Modify — add deprecated filter | 3 |
| `src/genesis/db/crud/memory.py` | Modify — add deprecated filter to FTS5 | 3 |
| `src/genesis/db/schema/_tables.py` | Modify — update memory_metadata schema | 3 |
| `src/genesis/memory/retrieval.py` | Verify — deprecated filter cascades correctly | 3 |
| `src/genesis/observability/_call_site_meta.py` | Modify — add dream_cycle_synthesis | 3 |
| `config/model_routing.yaml` | Modify — add dream_cycle_synthesis chain | 3 |
| `tests/test_memory/test_dream_cycle.py` | Create — unit tests | 3 |
| `tests/test_db/test_memory_links_crud.py` | Create/modify — prune_weak tests | 1 |

---

## Verification Plan

### Tier 1 Verification
1. `pytest tests/test_db/test_memory_links_crud.py -v` — weak link pruning
2. Manual: create old weak links, run prune, verify count
3. Manual: create old `.jsonl` files, run archival, verify `.gz` created

### Tier 3 Verification
1. `pytest tests/test_memory/test_dream_cycle.py -v` — unit tests
2. **Dry-run against live data:** Run dream cycle in dry-run mode against the
   production episodic_memory collection. Review cluster report:
   - How many clusters found?
   - Size distribution (2-3 vs 4-10 vs 10+)?
   - Sample cluster contents — do they actually belong together?
3. **Single live merge (supervised):** Enable live mode, cap at 1 cluster.
   Verify: synthesis stored correctly, originals deprecated, retrieval filters
   working, rollback works.
4. **Full run:** Enable with cap=100. Review observation report next morning.
5. **Rollback test:** Pick one run_id, execute rollback, verify originals
   un-deprecated and synthesis deleted.

### Regression Checks
- Memory recall still returns results (deprecated filter not over-filtering)
- Proactive hook still surfaces memories
- Essential knowledge generation still works
- FTS5 search still returns results
- `memory_synthesize` MCP tool still works independently

---

## Open Questions for Implementation

1. **Linking helper extraction:** `memory_synthesize` MCP tool creates `extends`
   links to source memories. Should dream cycle reuse this exact linking, or
   create a different link type (e.g., `consolidated_from`)? Leaning toward
   `consolidated_from` for semantic clarity.

2. **Wing/room reclassification:** If the LLM synthesis recommends a different
   wing/room than the originals, store with the recommended classification and
   add tag `"reclassified_by:dream_cycle"`. Tentative yes.

3. **Proactive hook interaction:** After consolidation, synthesized memories
   should surface preferentially. This happens naturally since deprecated
   originals are filtered out and the synthesis contains all their information.

4. **GC wrapping in schedule_maintenance:** The PR #352 review identified that
   GC calls run sequentially without individual try/except. Implement this
   wrapping as part of Tier 1 work (it's the right scope).

---

## Key Numbers (snapshot 2026-05-14)

| Metric | Value |
|---|---|
| episodic_memory points | ~19,000 |
| Daily growth rate | ~734/day |
| memory_links | 54,631 |
| Estimated mergeable pairs (5%) | ~950 clusters |
| Run cap | 100 clusters/run |
| Estimated weeks to steady state | ~10 (at 100/week) |
| Cost per run (free-tier) | $0 |
| Cost per run (paid fallback) | < $1 |
