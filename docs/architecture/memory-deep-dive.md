# Memory: How It Works

4-layer hybrid retrieval — Qdrant vector search + SQLite FTS5 fused via
Reciprocal Rank Fusion. Activation scoring, graceful degradation, and
taxonomy-aware organization across 10,500+ memories.

---

## Why this subsystem exists

LLM agents lose context between conversations. The two common approaches both
have problems: stuff everything into the context window (doesn't scale past a
few sessions), or vector search alone (misses keyword-exact matches, fails when
embeddings are unavailable).

Genesis runs 24/7 across many sessions. A decision from three weeks ago has to
surface when it's relevant today. That requires a retrieval system that handles
semantic similarity AND exact-term lookup AND knows which memories are still
relevant versus stale.

---

## 4 Layers

```
L1: Essential Knowledge (~300 tokens, injected at every session start)
    Pure DB queries — no LLM, no network dep. Always available.
    Content: active context, recent decisions, wing index.
    Updated: after each foreground session ends.
        │
        ▼ (injected automatically)
L2: Proactive Recall (per-prompt, <1.5s budget)
    UserPromptSubmit hook → FTS5 + Qdrant → inject top 3 memories.
    Intent tracking, pivot detection, file-context augmentation.
    Graceful: FTS5-only if embeddings unavailable.
        │
        ▼ (explicit query)
L3: Deep Search (on-demand, ~1-2s)
    Full RRF pipeline: 4 ranked signals fused.
    Wing/room filtering, activation gating, provenance tracking.
        │
        ▼ (ingest)
L4: Knowledge Pipeline
    External domain knowledge, separate collection.
    Idempotent upsert on (project, domain, concept).
    Stable unit IDs across re-ingestion.
```

Each layer answers different questions. L1 answers "what are we working on?"
without burning any retrieval budget. L2 surfaces relevant context automatically.
L3 handles explicit deep searches. L4 manages external reference knowledge.

---

## Hybrid Retrieval: RRF Fusion

Vector search and keyword search are complementary. Vector finds semantically
similar content; FTS5 finds exact matches. Combining them catches what either
alone would miss.

### Formula

```python
def _rrf_fuse(ranked_lists: list[list[str]], *, k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, memory_id in enumerate(ranked, 1):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + rank)
    return scores
```

Four ranked lists are fused:
1. Qdrant vector similarity (cosine, 1024 dims)
2. FTS5 text search (porter stemming + ASCII tokenization)
3. Activation score ranking (recency × confidence × connectivity)
4. Intent-aware ranking (domain bias when query intent is detectable)

k=60 normalizes contributions — rank-1 in any list contributes `1/61 = 0.016`.
A memory appearing in multiple lists accumulates score from each.

### Full Retrieval Pipeline

```
Query
 │
 ├─ [Embed query] (DeepInfra → DashScope → Ollama)
 │       └─ On failure: skip vector, FTS5-only
 │
 ├─ [Qdrant search] (if embedding succeeded)
 │       ├─ 3x candidate_limit results
 │       └─ Filter by wing/room at query time
 │
 ├─ [FTS5 search] (always)
 │       └─ Porter stemming: "recover" matches "recovery", "recovering"
 │
 ├─ [Activation scoring] (all candidates)
 │       └─ score = confidence × recency × weighted_factors × class_weight
 │
 ├─ [Intent classification]
 │       └─ SYSTEM_LOOKUP → bias toward infrastructure wing
 │
 └─ [RRF fusion] → sort → filter → return top K
         └─ Increment retrieved_count on each result
```

---

## Activation Scoring

Raw retrieval score doesn't account for whether a memory is still relevant.
A highly similar memory from six months ago is usually less useful than a
moderately similar one from last week.

```
activation = confidence × recency × (0.5 + 0.3×access + 0.2×connectivity) × class_weight
```

**Recency** — exponential decay with source-aware half-life:

```python
recency = exp(-0.693 * age_days / half_life)
```

| Source | Half-Life | Reason |
|--------|-----------|--------|
| session_extraction | 60 days | Conversational decisions stay relevant longer |
| deep_reflection | 45 days | Strategic insights |
| reflection | 30 days | Routine observations |
| default | 30 days | General content |

Memories tagged with proper nouns (capitalized) get 2× half-life. "Qdrant
configuration" decays slower than "tried a different approach."

**Access frequency** — logarithmic, caps at 20 retrievals:
```python
access_freq = min(1.0, log(1 + retrieved_count) / log(21))
```

**Connectivity** — graph edges in `memory_links`:
```python
connectivity = min(1.0, log(1 + link_count) / log(11))
```

**Class weight** — rules matter more than URL pointers:
```python
CLASS_WEIGHTS = {"rule": 1.3, "fact": 1.0, "reference": 0.7}
```

Classification: ALL-CAPS imperatives (ALWAYS, NEVER, MUST) → rule.
URL or "documented at" keywords → reference. Everything else → fact.

---

## Wing/Room Taxonomy

Memory is classified into structural domains at store time:

```
memory/          retrieval, extraction, store, embeddings, activation
learning/        skills, evolution, calibration, procedures, observations
routing/         model_selection, call_sites, circuit_breakers, providers
infrastructure/  guardian, sentinel, health, database, runtime, scheduler
channels/        telegram, dashboard, openclaw, inbox, mail
autonomy/        tasks, permissions, approval, protected_paths
general/         uncategorized
```

Classification uses tiered confidence: file path patterns (0.9) > keywords (0.7)
> tags (0.6) > source pipeline (0.5) > fallback (0.1).

Enables contextual retrieval: searching within `routing/circuit_breakers` cuts
noise from the full 10K+ store.

---

## Graceful Degradation

The embedding provider chain (Ollama → DeepInfra → DashScope) can fail entirely.
The system never blocks on it:

**Store path:**
```
Embed → Success: Qdrant upsert + FTS5 write
      → Failure: FTS5 write only + queue to pending_embeddings
                 (full provenance preserved: session_id, transcript_path, line_range)
```

**Recall path:**
```
Embed → Success: Full hybrid retrieval (4 signals)
      → Failure: FTS5 + activation + intent only (3 signals)
                 RRF still works with fewer lists
```

`pending_embeddings` preserves everything needed to reconstruct the vector later.
A recovery worker processes the queue asynchronously when embeddings come back.

---

## Graph Linking

Memories form a graph via `memory_links` (8,000+ edges). Links are created
automatically on store:

```python
async def auto_link(self, memory_id, vector, similarity_threshold=0.75, max_links=5):
    for neighbor in qdrant_neighbors:
        if neighbor.score >= 0.90:
            create_link(memory_id, neighbor.id, "extends")
        elif neighbor.score >= 0.75:
            create_link(memory_id, neighbor.id, "supports")
```

Link types: supports, contradicts, extends, elaborates, discussed_in,
evaluated_for, decided, action_item_for, succeeded_by, preceded_by.

Recursive traversal (SQL CTE with cycle detection) enables graph-based
recall: find everything connected to a decision within N hops.

---

## Storage

**Two Qdrant collections, 1024 dimensions, cosine distance:**

- `episodic_memory` — all internal Genesis memories (conversations, decisions,
  reflections, evaluations). Decays over time. Subject to correction.
- `knowledge_base` — external domain knowledge. Authoritative, doesn't decay.
  Separate from episodic so retrieval can distinguish "something I once thought"
  from "documented best practice."

**SQLite tables:** `memory_fts` (FTS5 index), `memory_metadata`, `memory_links`,
`knowledge_units`, `knowledge_fts`, `pending_embeddings`.

---

## Key Files

| File | Purpose |
|------|---------|
| `src/genesis/memory/recall.py` | HybridRetriever, full RRF pipeline |
| `src/genesis/memory/store.py` | Store pipeline, dedup, embedding fallback |
| `src/genesis/memory/activation.py` | Activation scoring formula |
| `src/genesis/memory/embeddings.py` | Provider chain, two-level cache |
| `src/genesis/memory/taxonomy.py` | Wing/room classification |
| `src/genesis/memory/linker.py` | Auto-linking, graph traversal |
| `src/genesis/memory/knowledge_ingest.py` | Idempotent KB upsert |
| `src/genesis/memory/essential_knowledge.py` | L1 generation (DB-only) |
| `scripts/proactive_memory_hook.py` | L2 UserPromptSubmit hook |

---

## Design Decisions

**Why RRF over learned ranking?**
RRF is deterministic, stateless, composable. Adding a 5th signal is one line.
ML ranking would be marginally better but requires training data and a model
to maintain.

**Why separate episodic and knowledge collections?**
Different lifecycle. Episodic memory decays, gets corrected, reflects internal
state. Knowledge is authoritative external content — it doesn't decay, it gets
re-ingested. Mixing them makes retrieval unable to distinguish the two.

**Why activation scoring vs. just recency?**
Recency alone under-weights important old decisions. A steering rule from month
one is more important than a casual observation from yesterday. Activation
combines recency with confidence, access patterns, and graph connectivity.

**Why FTS5 as the always-available fallback?**
Zero external dependencies — compiled into SQLite. If Qdrant is down, embeddings
are unavailable, network is gone, FTS5 still works. It's the floor that guarantees
memory never fully fails.

---

## V4 Targets

- **Contradiction detection**: auto-flag when a new memory contradicts an existing one
- **Decay curve tuning from feedback**: adjust half-lives based on observed utility,
  not just source category
- **Hierarchical summarization**: compress high-density memory regions into fewer
  semantic summaries as the store grows
