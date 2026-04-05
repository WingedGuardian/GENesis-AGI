# Genesis v3 — Memory Collection Architecture

**Status:** Active | **Last updated:** 2026-04-03


## Overview

Genesis uses two Qdrant vector collections for long-term memory, plus SQLite
tables for structured data. This document describes the purpose and routing
rules for each collection.

## Qdrant Collections

### episodic_memory

**Purpose:** ALL internal Genesis memory — conversations, decisions, evaluations,
extracted facts, session bookmarks, and reflection outputs.

**Vector config:** 1024 dimensions, Cosine distance (qwen3-embedding:0.6b)

**Writers:**
- `MemoryStore.store()` — default collection for all `memory_type` values
- Session extraction pipeline — extracted entities, decisions, evaluations
- Reflection/perception pipeline — reflection outputs stored as episodic
- `memory_store` MCP tool — user/agent-initiated memory storage
- `memory_extract` MCP tool — batch extraction storage

**Scope tags in payload:**
- `scope: "user"` — Conversations, decisions, evaluations, facts (default)
- `scope: "internal"` — Reflection outputs, extraction metadata

**Classification in payload (added 2026-04-03):**
- `memory_class: "rule"` — Actionable instructions (NEVER, ALWAYS, MUST).
  Gets 1.3x activation boost. Two-tier formatting in proactive hook (always full).
- `memory_class: "fact"` — Informational (default). Neutral weight.
- `memory_class: "reference"` — Pointers to external resources. 0.7x weight.

Auto-classified at store time by `classification.classify_memory()` based on
content heuristics + CC file memory type. Can be overridden via `memory_class`
param on `memory_store` MCP tool or `MemoryStore.store()`.

**Searched by:** proactive memory hook (UserPromptSubmit), `memory_recall` MCP,
`memory_proactive` MCP, `HybridRetriever.recall()`.

### knowledge_base

**Purpose:** External domain knowledge ONLY — data from capability modules
(crypto, prediction markets, etc.) and explicitly ingested domain documentation.

**Vector config:** 1024 dimensions, Cosine distance (same as episodic_memory)

**Writers:**
- `knowledge_ingest` MCP tool — explicit domain knowledge ingestion
  (passes `collection="knowledge_base"` to bypass `_COLLECTION_MAP`)
- Pipeline orchestrator — domain module signal storage
  (passes `collection="knowledge_base"` to bypass `_COLLECTION_MAP`)

**Scope tags in payload:**
- `scope: "external"` — always

**Searched by:** `knowledge_recall` MCP, `memory_recall(source="knowledge")`.

**NOT searched by:** proactive memory hook (user-facing recall searches
`episodic_memory` only).

## Routing Rules

```python
# In src/genesis/memory/store.py
_COLLECTION_MAP = {
    "episodic": "episodic_memory",
    "knowledge": "episodic_memory",  # Internal knowledge stays with episodic
}
```

The `store()` method accepts an optional `collection` parameter that bypasses
the map. Only two callers use this:
1. `knowledge_ingest` — `collection="knowledge_base"`
2. Pipeline orchestrator — `collection="knowledge_base"`

All other callers use the default map, which routes everything to
`episodic_memory`.

## SQLite Tables

| Table | Purpose | Linked to Qdrant? |
|-------|---------|-------------------|
| `memory_fts` | FTS5 full-text search fallback | Yes — `memory_id` = Qdrant point ID |
| `knowledge_units` | Structured domain knowledge metadata | Yes — `qdrant_id` = knowledge_base point ID |
| `knowledge_fts` | FTS5 search for knowledge_units | Yes — `unit_id` = knowledge_units.id |
| `observations` | Transient working memory, lifecycle-tracked | No — SQLite only |
| `procedural_memory` | Learned procedures, versioned | No — SQLite only |
| `session_bookmarks` | Session resumption markers | Partially — also stored in episodic_memory |
| `memory_links` | Graph connections between memories | References Qdrant point IDs |
| `memory_metadata` | Timestamps, confidence, embedding_status, memory_class | Yes — `memory_id` = Qdrant point ID |
| `pending_embeddings` | Embedding failure recovery queue (with provenance) | Pending → Qdrant on recovery |

## Proactive Hook File-Context Awareness (added 2026-04-03)

A PostToolUse hook (`scripts/file_context_hook.py`) tracks file paths from
Read/Edit/Write/Glob/Grep operations, writing them to
`~/.genesis/sessions/{session_id}/recent_files.json`. The proactive memory
hook reads this file and decomposes paths into keywords that augment the
FTS5 query, improving retrieval relevance for the current work context.

## History

Before the routing fix (2026-03-24), `_COLLECTION_MAP` routed
`memory_type="knowledge"` to `knowledge_base`. This caused 539 internal
entries (evaluations, session facts) to accumulate in the domain store.
The fix: route all internal knowledge to `episodic_memory`, give domain
writers explicit collection overrides, migrate existing entries, and
clear `knowledge_base` for its intended purpose.

**Memory rebalance (2026-04-03):**
- Added `memory_class` (rule/fact/reference) to Qdrant payloads and
  `memory_metadata` table. Auto-classified at store time.
- Rules get 1.3x activation boost; references get 0.7x.
- Proactive hook uses two-tier formatting (full for rank 1 + rules, compact
  for lower-ranked non-rules).
- PostToolUse file-context hook tracks recent files per session; proactive
  hook augments FTS5 queries with file-derived keywords.
- Purged ~1,600 orphaned memory_links (24% of graph) via migration.
- Added provenance columns to `pending_embeddings` so recovery worker
  preserves source_session_id, transcript_path, etc.
- Query expansion in `HybridRetriever.recall()` defaulted to off
  (`expand_query_terms=False`). Opt-in for callers that need it.

---

## Related Documents

- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Phase 5: memory operations
- [genesis-v3-autonomous-behavior-design.md](genesis-v3-autonomous-behavior-design.md) — Memory in behavior architecture
