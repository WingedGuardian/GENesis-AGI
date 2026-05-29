# Design Spec: Honcho Integration + Database Adapter

**Date:** 2026-05-29
**Status:** Draft
**Scope:** Phase 1 (Honcho as separate service) + Phase 2 (DB adapter + Postgres groundwork)

---

## Context

Genesis needs user-modeling capabilities that go beyond its current approach:
specifically, systematic per-conversation user model extraction, an observation
type system with reasoning chains (deductive/inductive/contradiction linked via
premise graphs), dream specialists that build higher-order insights, and a
dialectic query interface that can reason about the user on demand.

Honcho (Plastic Labs, AGPL-3.0, production-ready) provides all of these. Rather
than building a knockoff adapted to SQLite, Genesis will run Honcho as a
separate service with its own Postgres — the same pattern used for Qdrant today.

Separately, Genesis will build a thin database adapter to prepare for a possible
future Postgres migration of its own relational data. This is insurance, not
commitment — it makes the migration feasible without requiring it.

---

## Phase 1: Honcho Integration (Separate Service)

### 1.1 Architecture

```
Genesis (SQLite, aiosqlite)
    |
    ├── Qdrant (localhost:6333)
    |   └── 27K+ episodic + knowledge memories
    |       Wings/rooms, activation scoring, RRF fusion
    |
    └── Honcho (localhost:8000, own Postgres)
        ├── API (FastAPI, REST)
        ├── Deriver (background worker, LLM extraction)
        ├── Dreamer (consolidation specialists)
        └── PostgreSQL + pgvector
            └── User observations, reasoning chains, peer cards
```

Honcho is a **peer service**, not an embedded library. Genesis talks to it via
REST API, the same way it talks to Qdrant. No shared database, no shared schema.

### 1.2 Services to Install

| Service | Runtime | Port | Managed By |
|---------|---------|------|------------|
| PostgreSQL 15 + pgvector | Docker or systemd | 5432 | systemd |
| Honcho API | Python (FastAPI) | 8000 | systemd |
| Honcho Deriver | Python (background worker) | — | systemd |
| Redis (optional) | Docker or systemd | 6379 | systemd |

Resource impact: ~2-4 GB additional RAM on a 32 GB system. No GPU.

### 1.3 Honcho Data Model

Honcho uses a peer-centric model:

- **Workspace**: Top-level isolation container. One workspace for Genesis.
- **Peers**: Two peers — "Jay" (human) and "Genesis" (AI). Both are first-class
  entities. Honcho builds models of both.
- **Sessions**: Each Genesis foreground conversation maps to a Honcho session.
- **Messages**: Conversation turns piped from Genesis to Honcho after each session.

### 1.4 Integration Points

#### 1.4.1 Conversation Transcript Pipeline

**When:** After each foreground CC session ends (or at session checkpoints).

**Trigger mechanism:** Genesis does not currently have a "session ended" hook.
Two options for detecting session completion:
- **Option A (preferred):** The awareness loop already tracks `cc_sessions` and
  detects session state changes. Add a check: if a session that was `active` is
  now `completed` and hasn't been sent to Honcho, pipe its transcript.
- **Option B:** A periodic scan (e.g., every 5 minutes) that checks for new
  completed sessions not yet sent to Honcho. Simpler but less responsive.

**How:** A new Genesis component (`src/genesis/honcho/adapter.py`) reads the
session transcript and sends messages to Honcho's API:

```
Session state change detected → read transcript → POST /workspaces/{ws}/sessions/{s}/messages
```

The Honcho deriver then asynchronously:
1. Extracts explicit observations (one LLM call per batch)
2. Embeds observations in pgvector
3. Updates the peer card
4. Queues dream consolidation when idle

**Transcript source:** CC session transcripts are available at
`~/.claude/projects/-home-ubuntu-genesis/{session_id}.jsonl`. Parse assistant
and user turns, map to Honcho message format.

**Cost:** The deriver uses a cheap model (configurable — default gpt-5.4-mini
equivalent). One LLM call per conversation, extracting atomic facts. Estimated
cost: $0.001-0.01 per conversation.

#### 1.4.2 Ego Context Injection

**Where:** `src/genesis/ego/user_context.py` — `UserEgoContextBuilder`

**What changes:** Add a new section to the ego's user context that queries
Honcho for a synthesized user representation:

```python
# In UserEgoContextBuilder.build()
honcho_context = await honcho_client.get_peer_representation("Jay")
honcho_card = await honcho_client.get_peer_card("Jay")
# Inject into ego context alongside existing user_model_cache data
```

This supplements (does NOT replace) the existing user model pipeline. The ego
sees both Genesis's synthesized user model AND Honcho's peer representation.
Over time, the two should converge — if they diverge, that's a signal that
one or both need calibration.

#### 1.4.3 Dialectic Query MCP Tool

**New MCP tool:** `user_model_query` (or `honcho_query`)

```
user_model_query(question: str, reasoning_level: str = "medium") -> str
```

Calls Honcho's dialectic API: `POST /workspaces/{ws}/peers/{peer}/chat`

Available in foreground sessions and ego cycles. Enables questions like:
- "What's the best way to present this technical decision to Jay?"
- "What are Jay's current priorities based on recent conversations?"
- "How does Jay typically respond to architectural proposals?"

Reasoning levels map to Honcho's 5 levels: minimal ($0.001), low ($0.01),
medium ($0.05), high ($0.10), max ($0.50).

#### 1.4.4 Session-Start Context Prewarm

**When:** On foreground session start (UserPromptSubmit hook or session init).

**What:** Fire a background dialectic query at medium depth so the first turn
has immediate access to synthesized user context. This is what Hermes does —
prewarm the dialectic so turn 1 isn't cold.

**How:** The existing SessionStart hook can trigger an async Honcho query.
Result is cached and available for proactive recall injection.

### 1.5 Configuration

New config section in `~/.genesis/config/genesis.yaml`:

```yaml
honcho:
  enabled: true
  api_url: http://localhost:8000
  workspace: genesis
  peers:
    user: Jay
    ai: Genesis
  deriver:
    model: gpt-5.4-mini  # or a free-tier model via Genesis routing
    batch_max_tokens: 25000
  dialectic:
    default_level: medium
    prewarm_on_session_start: true
  dream:
    enabled: true
    idle_timeout_minutes: 60
    min_interval_hours: 8
```

### 1.6 What Stays Unchanged

- Genesis's memory system (Qdrant + FTS5 + SQLite) — untouched
- Genesis's observation system — untouched (Honcho observations are separate)
- Genesis's dream cycle — runs independently of Honcho's dream cycle
- USER.md / USER_KNOWLEDGE.md — still generated by Genesis's synthesis cycle
- Essential knowledge — still generated from Genesis's data

Honcho is additive. It provides a new user-modeling signal that supplements
existing signals. Nothing is removed or replaced.

### 1.7 Future Consolidation (Optional, Not In Scope)

If the two systems prove redundant over time, the following consolidations
could be considered (but are explicitly NOT part of this spec):

- Replace `user_model_cache` synthesis with Honcho peer representation
- Replace ad-hoc `user_signal` observations with Honcho's typed observations
- Replace USER_KNOWLEDGE.md auto-synthesis with Honcho peer card
- Merge Genesis dream cycle with Honcho dream cycle

These are evaluations for 3+ months after integration, once we have data on
how well the two systems complement each other.

---

## Phase 2: Database Adapter + Postgres Groundwork

### 2.1 Purpose

Build a thin database abstraction layer so that Genesis's 55 CRUD modules
(10,232 LOC) can be incrementally migrated from raw aiosqlite to an
engine-agnostic interface. This makes a future Postgres migration feasible
without committing to it.

### 2.2 The Adapter Interface

```python
# src/genesis/db/adapter.py

class DatabaseAdapter(Protocol):
    """Thin interface between business logic and database engine."""

    async def execute(self, query: str, params: dict | None = None) -> None:
        """Execute a write query (INSERT, UPDATE, DELETE)."""
        ...

    async def fetch_one(self, query: str, params: dict | None = None) -> Row | None:
        """Fetch a single row."""
        ...

    async def fetch_all(self, query: str, params: dict | None = None) -> list[Row]:
        """Fetch all matching rows."""
        ...

    async def fetch_val(self, query: str, params: dict | None = None) -> Any:
        """Fetch a single scalar value."""
        ...

    async def transaction(self) -> AsyncContextManager:
        """Begin a transaction. Commits on clean exit, rolls back on exception."""
        ...

    def json_get(self, column: str, path: str) -> str:
        """Return SQL expression for JSON field access.
        SQLite: json_extract({column}, '$.{path}')
        Postgres: {column}->>{path}
        """
        ...

    def upsert_clause(self, conflict_columns: list[str],
                      update_columns: list[str] | None = None) -> str:
        """Return SQL clause for upsert.
        SQLite: INSERT OR IGNORE / INSERT OR REPLACE
        Postgres: ON CONFLICT (cols) DO NOTHING / DO UPDATE SET ...
        """
        ...
```

### 2.3 SQLiteAdapter Implementation

The first (and initially only) adapter implementation wraps the existing
`SerializedConnection`:

```python
class SQLiteAdapter(DatabaseAdapter):
    """Wraps aiosqlite with the adapter interface."""

    def __init__(self, db_path: str):
        self._conn = SerializedConnection(db_path)

    async def execute(self, query: str, params: dict | None = None) -> None:
        # Translate :name params to ? positional for aiosqlite
        translated_query, positional_params = self._translate(query, params)
        await self._conn.execute(translated_query, positional_params)

    async def fetch_one(self, query: str, params: dict | None = None) -> Row | None:
        translated_query, positional_params = self._translate(query, params)
        cursor = await self._conn.execute(translated_query, positional_params)
        return await cursor.fetchone()

    # ... etc
```

### 2.4 Migration Strategy for CRUD Files

**Approach:** Incremental, file-by-file. No big bang.

**Priority order** (highest-traffic and most SQLite-specific first):
1. `memory_metadata.py` — FTS5 queries, heaviest traffic
2. `knowledge_units.py` — FTS5 queries
3. `observations.py` — high traffic, JSON operations
4. `ego_proposals.py` — ego-critical path
5. `cc_sessions.py` — high traffic
6. Remaining 50 files — as touched for other work

**Per-file migration steps:**
1. Replace `aiosqlite.connect(DB_PATH)` with adapter injection
2. Convert `?` params to `:name` params
3. Replace `INSERT OR IGNORE` with `adapter.upsert_clause()`
4. Replace `json_extract()` with `adapter.json_get()`
5. Test that existing behavior is preserved

**Estimated effort per file:** 30-60 minutes for simple files, 2-4 hours for
FTS-heavy files.

### 2.5 FTS5 Abstraction

The most important dialect-specific abstraction. FTS5 is used in two places:

1. `memory_fts` — 27K rows, used in hybrid search (FTS5 + Qdrant + RRF)
2. `knowledge_fts` — 1.7K rows, used in knowledge recall

**Interface:**

```python
class TextSearchProvider(Protocol):
    """Full-text search abstraction."""

    async def search(self, query: str, table: str,
                     limit: int = 20) -> list[TextSearchResult]:
        """Search with BM25 ranking. Returns scored results."""
        ...

    async def index(self, doc_id: str, content: str, table: str) -> None:
        """Add or update a document in the search index."""
        ...

    async def delete(self, doc_id: str, table: str) -> None:
        """Remove a document from the search index."""
        ...
```

**SQLite implementation:** Uses FTS5 MATCH + bm25(). Wraps `_prepare_fts5()`.
**Future Postgres implementation:** Uses `tsvector`/`tsquery` + `ts_rank()`.

This abstraction isolates the ~8 FTS-specific queries in the codebase behind
a clean interface. The memory retrieval pipeline (`memory_recall`) calls
`TextSearchProvider.search()` instead of raw FTS5 SQL.

### 2.6 Other Groundwork Items

**PRAGMA consolidation:**
Move all SQLite PRAGMAs into `SQLiteAdapter.__init__()`:
- `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=5000`
- `PRAGMA wal_checkpoint(PASSIVE)` (awareness loop)

Currently scattered across ~10 files. Centralizing them means a future
`PostgresAdapter` simply doesn't set them.

**Migration runner:**
Update `src/genesis/db/migrations/runner.py` to accept a `DatabaseAdapter`
instead of raw aiosqlite connection. Replace `BEGIN IMMEDIATE` with
`adapter.transaction()`. Replace `PRAGMA table_info()` with
`adapter.table_exists()` / `adapter.column_exists()`.

**Backup scripts:**
No change needed now. When/if Postgres migration happens, swap
`sqlite3 .dump` for `pg_dump`. Trivial.

### 2.7 What This Does NOT Do

- Does NOT migrate Genesis to Postgres (that's a separate future decision)
- Does NOT change the database schema
- Does NOT affect runtime behavior — same queries, same results, same perf
- Does NOT add Postgres as a dependency
- Does NOT touch Qdrant or the vector search pipeline

### 2.8 Success Criteria

**Phase 1:**
- Honcho API + Deriver + Postgres running as systemd services
- Foreground session transcripts flowing to Honcho after each session
- `user_model_query` MCP tool functional (can ask questions about the user)
- Ego context includes Honcho peer representation
- Honcho dream cycle running on configured schedule
- No degradation to existing Genesis functionality

**Phase 2:**
- DatabaseAdapter Protocol defined and SQLiteAdapter implemented
- TextSearchProvider Protocol defined and FTS5SearchProvider implemented
- At least the top 5 highest-traffic CRUD files migrated to adapter
- All PRAGMAs consolidated into adapter init
- All existing tests pass unchanged
- Migration runner accepts adapter

---

## Sequencing

| Phase | Effort | Dependencies |
|-------|--------|-------------|
| 1a: Install Honcho services | 1 day | None |
| 1b: Transcript pipeline (Genesis → Honcho) | 2-3 days | 1a |
| 1c: MCP tool + ego context integration | 2 days | 1a |
| 1d: Session prewarm + configuration | 1 day | 1b, 1c |
| 2a: Adapter interface + SQLiteAdapter | 2-3 days | None (parallel with 1) |
| 2b: FTS abstraction | 2-3 days | 2a |
| 2c: PRAGMA consolidation + migration runner | 1 day | 2a |
| 2d: Incremental CRUD migration (top 5 files) | 3-5 days | 2a |

**Total Phase 1:** ~1 week
**Total Phase 2:** ~2 weeks (can overlap with Phase 1)

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Honcho deriver LLM costs accumulate | Medium | Use cheapest viable model; gate on session length (skip trivial sessions) |
| Dual user models (Genesis + Honcho) diverge | Low | Monitor divergence; consolidation is a future option, not a requirement |
| Postgres adds operational burden | Low | Single-purpose (Honcho only); automated backups via cron |
| Adapter migration introduces regressions | Medium | File-by-file with existing test coverage; no behavioral changes |
| FTS5 abstraction performance regression | Medium | Benchmark before/after; the abstraction is a thin wrapper, not a rewrite |
| Honcho upstream breaking changes | Low | Pin version; evaluate upgrades deliberately |

---

## Decision Record

- **Honcho as separate service, not embedded:** Avoids Postgres migration for
  Genesis core. Same pattern as Qdrant. Clean service boundary.
- **Supplement, not replace:** Honcho adds to Genesis's user modeling; does not
  replace existing memory/observation systems. Consolidation is a future
  evaluation, not a commitment.
- **Adapter, not ORM:** Thin interface preserves hand-written SQL (well-optimized,
  well-understood) while enabling engine portability. ORM rewrite would be
  multi-week with uncertain benefit.
- **Qdrant stays:** Qdrant and pgvector serve different purposes (Genesis memories
  vs Honcho user observations). No consolidation needed or planned.
- **Incremental migration:** CRUD files migrate to adapter one at a time, not
  big-bang. Reduces risk, allows course correction.
