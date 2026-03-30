# Phase 0: The Foundation — Schema Before Code

*Completed 2026-03-02. 170 tests.*

---

## What We Built

Phase 0 is the least glamorous and most important phase in Genesis. Before a single LLM call, before any signal processing or reflection or memory retrieval, we built the data layer: 13 database tables, 13 CRUD modules, a Qdrant vector wrapper (1024-dimensional embeddings), and stub interfaces for all 4 MCP servers with 24 tool definitions.

No intelligence lives here. No LLM is consulted. This is pure schema — the skeletal structure that every subsequent phase hangs its weight on.

## Why Schema Before Code Matters

Most AI agent projects start with the exciting parts: hooking up an LLM, building a chat interface, generating responses. The data model is an afterthought — a table here, a column there, schema migrations piling up as the architecture shifts underneath.

Genesis inverted this. We started with the question: **what does an intelligent system need to remember, track, and reason about?** The answer became 13 tables covering memory storage, observation tracking with utility fields, execution traces, surplus staging, signal weights, capability gaps, procedural memory, user model cache, speculative claims, autonomy state, outreach history, and brainstorm logs.

Every table includes groundwork fields for V4 and V5 features that don't exist yet. Some columns will stay empty for months. That's by design — adding a column to a populated table is an operation; having it ready from day one is free.

## Key Design Decisions

**Full schema up front, not incremental.** We defined every table the design documents called for, even tables that wouldn't be populated until Phase 6 or Phase 9. This meant Phase 5 (Memory) could focus entirely on retrieval algorithms rather than schema design. Phase 6 (Learning) could focus on classification logic rather than table creation. Each subsequent phase arrived to find its storage layer already waiting.

**MCP server interfaces before implementations.** All 4 MCP servers — memory, recon, health, outreach — had their tool interfaces defined with type signatures and documentation. The implementations were stubs, but the contracts were real. This meant any phase that needed to call a memory operation or queue an outreach message could code against a stable interface from day one.

**Qdrant from the start.** Agent Zero's default memory uses FAISS (in-memory, file-backed). Genesis went directly to Qdrant — a proper vector database with filtering, payload storage, and collection management. This eliminated a migration that would have been painful later, and gave us production-grade vector search from Phase 0.

**Utility tracking on everything.** Observations track `retrieved_count` and `influenced_action`. Procedures track `confidence`, `invocation_count`, and `success_rate`. Surplus insights track promotion status. This instrumentation is invisible to the user but essential to the system — without it, Genesis cannot evaluate whether its own outputs are useful. You cannot improve what you do not measure, and measurement infrastructure must exist before the things it measures.

**Groundwork fields for future versions.** Several tables include columns that will not be populated until V4 or V5. Adaptive signal weights, learned channel preferences, identity evolution proposals — the schema supports them now. This is a deliberate trade: a few empty columns cost nothing, but adding columns to populated tables during a live system requires migration work and carries risk. The schema is a bet on the roadmap, and bets are cheapest when placed early.

## What We Learned

The primary lesson from Phase 0 is that **data models are architectural decisions**. The shape of your tables determines the questions your system can ask about itself. If you don't have a `retrieved_count` column on observations, you can never ask "which observations actually influenced decisions?" If you don't have a `success_rate` on procedures, you can never ask "is this learned behavior working?"

Genesis's ability to evaluate itself — to look in the mirror and see something honest — starts here, in the mundane act of defining the right columns on the right tables. Everything intelligent that Genesis does later is downstream of these 13 tables.

The other lesson was about MCP server contracts. Defining tool interfaces before implementations meant that every consumer of those interfaces — and there would be many, across all subsequent phases — coded against a stable contract from day one. When Phase 5 finally implemented the memory-mcp tools for real, nothing upstream needed to change. The stubs were replaced with working code, and the callers never knew the difference. That stability was worth the upfront investment of thinking through every tool signature before writing the first line of business logic.

The foundation is invisible, and that is exactly the point.
