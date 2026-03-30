# Phase 5: Memory Operations — Design Document

**Date:** 2026-03-08
**Status:** Implemented (Step 1)
**Scope:** Genesis memory-mcp as independent system

## Problem

Phase 0 created 13 tables, CRUD modules, Qdrant wrapper, and 4 MCP server stubs
(24 tools total). The memory-mcp had 9 stub tools raising NotImplementedError.
Genesis needs operational memory: store, retrieve, link, and evolve a user model.

## Design Decisions

1. **Two-step approach**: Step 1 builds independent Genesis memory-mcp (COMPLETE).
   Step 2 adapts AZ's Memory class to delegate to genesis.memory modules,
   replacing FAISS with Qdrant+FTS5 backend (IN PROGRESS).

2. **Activation scoring**: On-the-fly ACT-R-inspired formula from existing fields.
   V5 reevaluation: check if on-the-fly still meets performance needs at scale.

3. **Memory linking**: `memory_links` join table with types: supports, contradicts
   (Phase 7), extends, elaborates (Phase 7). Similarity-based auto-linking at
   store time.

4. **User model synthesis**: Rule-based for Phase 5. Auto-accept deltas with
   confidence >= 0.7. Accumulate below; 3+ same field+value occurrences → accept.
   Phase 7 adds LLM batch for ambiguous cases.

5. **Embeddings**: Ollama `qwen3-embedding:0.6b` (1024-dim) primary via httpx.
   Mistral embed (native 1024-dim) fallback. Contextual enrichment:
   `"{memory_type}: {tags}: {content}"`.

6. **Hybrid retrieval**: Qdrant vector + FTS5 text + activation scoring, fused
   via Reciprocal Rank Fusion (RRF, k=60).

7. **Correction chain**: L2 individual self-correct + log. L5 pattern detection →
   user proposal. L6 systemic → Strategic proposal. Phase 7 Deep reflection audits
   recent programmatic memory operations.

## Architecture

```
src/genesis/memory/
├── __init__.py
├── types.py           # 5 frozen dataclasses
├── activation.py      # compute_activation() — pure math, no I/O
├── embeddings.py      # EmbeddingProvider: Ollama + Mistral fallback
├── linker.py          # MemoryLinker: auto_link at store time
├── retrieval.py       # HybridRetriever: RRF fusion pipeline
├── store.py           # MemoryStore: embed → Qdrant → FTS5 → auto-link
├── user_model.py      # UserModelEvolver: rule-based delta synthesis
```

## Activation Formula

```
recency = exp(-0.693 * age_days / half_life_days)
access_freq = min(1.0, log(1 + retrieved_count) / log(1 + 20))
connectivity = min(1.0, log(1 + link_count) / log(1 + 10))
final = confidence * recency * (0.5 + 0.3 * access_freq + 0.2 * connectivity)
```

Cold-start protection: 50% base weight ensures new memories (0 retrievals, 0
links) get half their confidence-recency score.

## MCP Tools Implemented

| Tool | Delegates to |
|------|-------------|
| `memory_recall` | HybridRetriever.recall() |
| `memory_store` | MemoryStore.store() |
| `memory_extract` | MemoryStore.store() per item |
| `memory_proactive` | HybridRetriever.recall() (low min_activation) |
| `memory_core_facts` | Observation query + activation sort |
| `memory_stats` | Qdrant info + FTS5 + link counts |
| `observation_write` | observations.create() |
| `observation_query` | observations.query() |
| `observation_resolve` | observations.resolve() |

## Integration Points

- **ContextAssembler**: Optional `UserModelEvolver` → populates `user_model` field
- **ResultWriter**: Optional `MemoryStore` → persists reflections to Qdrant+FTS5

## Future Work

- **Phase 7**: LLM batch for ambiguous user model deltas; Deep reflection audits
  of auto-accepted deltas and auto-created links
- **Step 2** (IN PROGRESS): AZ Memory adapter — memory.py delegates to
  genesis.memory modules. FAISS removed. Migration script. Rebase plan doc
  for eventual plugin conversion
- **V5**: Reevaluate on-the-fly activation scoring at scale
