# ADR 004: Memory Taxonomy — Wings and Rooms

**Status:** Accepted
**Date:** 2026-03-15

## Context

Genesis stores 10,000+ memories across diverse domains (infrastructure,
learning, channels, routing, autonomy, etc.). Flat tagging creates recall
noise — a query about "routing" might surface routing-infrastructure memories
alongside routing-model-selection memories with very different contexts.

## Decision

Memories are organized in a two-level taxonomy:
- **Wing** (top-level domain): infrastructure, learning, channels, memory,
  routing, autonomy, identity, career, architecture
- **Room** (specific topic within wing): e.g., infrastructure/health,
  learning/observations, routing/model_selection

Recall can filter by wing for domain-scoped search, or search globally.

## Consequences

**Benefits:**
- Domain-scoped recall reduces noise (searching "routing" in the routing wing
  returns model selection, not infrastructure routing)
- Proactive hook can bias toward the active wing when it detects domain context
- Dashboard can display memory distribution by wing
- Enables per-wing statistics and health monitoring

**Costs:**
- Classification burden — every memory needs wing/room assignment
- Cross-cutting concerns don't fit neatly (a routing bug affecting infrastructure)
- Taxonomy evolution requires migration when domains shift

**Why not flat tags:** Tags are composable but have no hierarchy. With 10K+
memories, flat search returns too many results. The wing/room structure gives
a first-pass filter that dramatically improves recall precision without
sacrificing the ability to search globally when needed.

**Why not deeper hierarchy:** Two levels is sufficient for current scale.
Adding a third level (wing/floor/room) would increase classification
complexity without meaningful recall improvement at 10K memories. Revisit
at 50K+.
