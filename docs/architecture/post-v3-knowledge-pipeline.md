# Post-v3: Knowledge Base & Ingestion Pipeline

**Date:** 2026-02-27
**Status:** Implemented (v3.0a5+ — content processors, distillation, orchestrator, dashboard, resume review)
**Decision source:** Conversation between user and Claude Code, 2026-02-27
**Behavioral layer:** `genesis-knowledge-autonomy.md` — defines the user-facing
principle (Genesis handles ingestion/organization/retrieval autonomously) that
this pipeline serves. This doc is the HOW; that doc is the WHEN and WHY.

---

## What This Is

A **knowledge base** system that lets Genesis ingest, distill, store, and retrieve
authoritative reference material — course content, specs, documentation, articles —
as immutable, project-scoped knowledge units. Distinct from episodic memory: knowledge
is treated as "law" (source of truth), not subject to consolidation, decay, or revision.

## What This Is NOT

- Not a replacement for memory (different lifecycle, different authority level)
- Not a general-purpose web scraper
- Not a document management system
- Not for conversation Q&A primarily — it's for **background agents, task execution,
  audit trails, and the Self-Learning Loop**

---

## Architecture Decision: Lives in memory-mcp

Knowledge base tools are a namespace within memory-mcp, not a separate server.
Rationale: shares Qdrant, embedder, FTS5, SQLite pool. Needs its own collection
and retrieval filter, not its own process. See memory-mcp section in
`genesis-v3-autonomous-behavior-design.md`.

---

## Storage Design

### Qdrant Collection: `knowledge_base`

Separate from `episodic_memory`. Same embedding model, different lifecycle rules.

**Vector payload metadata:**
```json
{
  "project_type": "cloud-engineering",
  "domain": "aws-vpc",
  "source_doc": "cloud-eng-course-module-3",
  "source_platform": "circle",
  "section_title": "VPC Subnet Configuration",
  "chunk_index": 3,
  "total_chunks": 47,
  "parent_chunk_id": "uuid-of-parent-section",
  "ingested_at": "2026-03-15T...",
  "source_date": "2026-01-10",
  "distillation_model": "claude-sonnet-4-6",
  "confidence": 0.85,
  "immutable": true
}
```

### FTS5 Table: `knowledge_fts`

Mirrors episodic_fts pattern but with `project_type` and `domain` columns for
filtered keyword search.

### SQLite Table: `knowledge_units`

Structured metadata + raw distilled text (enables re-embedding on model change
without re-ingestion from source).

```sql
CREATE TABLE knowledge_units (
    id TEXT PRIMARY KEY,
    project_type TEXT NOT NULL,
    domain TEXT NOT NULL,
    source_doc TEXT NOT NULL,
    source_platform TEXT,
    section_title TEXT,
    concept TEXT NOT NULL,
    body TEXT NOT NULL,          -- distilled knowledge (raw text for re-embedding)
    relationships TEXT,          -- JSON array of related concept IDs
    caveats TEXT,                -- JSON array of known limitations
    tags TEXT,                   -- JSON array of semantic tags
    confidence REAL DEFAULT 0.85,
    source_date TEXT,
    ingested_at TEXT NOT NULL,
    qdrant_id TEXT,              -- foreign key to vector
    embedding_model TEXT         -- tracks which model produced the vector
);
```

---

## Knowledge Unit Schema (LLM Output)

The distillation LLM produces these from raw content:

```json
{
  "concept": "VPC Subnet Configuration",
  "domain": "aws-vpc",
  "body": "A VPC subnet is a subdivision of the VPC CIDR range...",
  "relationships": ["vpc-overview", "route-tables", "nat-gateway"],
  "caveats": ["AZ mapping is account-specific, not universal"],
  "tags": ["aws", "networking", "vpc", "subnet"],
  "confidence": 0.90
}
```

---

## Ingestion Pipeline (5 Stages)

### Stage 1: Acquisition Agent
- Drives Playwright MCP for authenticated platforms (Circle, Thinkific)
- Extracts course structure (modules, lessons, resources)
- Downloads video files or finds transcript exports
- **Checkpoint/resume**: Tracks progress per-lesson; resumes on auth expiry
- **Platform notes:**
  - **Circle:** Community platform — content in Spaces, embedded video (Vimeo/Wistia/Loom). Requires per-embed-provider extraction logic.
  - **Thinkific:** Structured LMS — course/module/lesson hierarchy. Standard video hosting. Easier acquisition path.

### Stage 2: Transcription
- Whisper (local or API) for video/audio
- Direct text extraction for PDFs, markdown, slides
- **Visual content gap:** Periodic frame extraction + vision model for slide/diagram content. Merged with transcript timeline.
- Output: Raw timestamped text with speaker labels

### Stage 3: Distillation (LLM)
- Transforms raw transcript/text into structured knowledge units
- **NOT storage of raw content** — produces understanding, not reproduction
- Flags uncertainties, gaps, cross-references, conflicts
- Quality scoring: well-structured lectures distill cleanly; rambling walkthroughs get flagged for review
- **Cross-reference linking pass:** After all modules ingested, a second pass resolves inter-module references ("as we discussed in module 2")

### Stage 4: Storage
- Embed distilled text via existing Embedder (local nomic + cloud fallback)
- Store in Qdrant `knowledge_base` collection
- Index in FTS5 `knowledge_fts` table
- Store raw text + metadata in SQLite `knowledge_units`

### Stage 5: Validation
- Sample retrieval test: query known concepts, verify correct chunks returned
- Gap detection: identify modules that produced few/no knowledge units
- Staleness tagging: source_date + domain → auto-flag when domain moves fast

---

## Retrieval: Authority-Aware Protocol

### Authority Hierarchy (LLM prompt architecture, not code)

```
Level 1: Knowledge base (studied, verified source material)
   ↓ augmented by
Level 2: Genesis's trained knowledge (model weights)
   ↓ verified by
Level 3: Web search (current/live information)
```

### Prompt Pattern

```
## Knowledge Authority Protocol

When answering questions or executing tasks related to [project_type]:

1. FIRST consult the provided REFERENCE blocks from the knowledge base.
   These represent studied, verified source material. Treat as primary authority.

2. THEN apply your own reasoning and training knowledge to fill gaps.

3. If you detect UNCERTAINTY or CONFLICT between your knowledge and the
   references, flag it explicitly and search the web for resolution.

4. NEVER silently contradict reference material. If you disagree, say so
   with evidence.

5. Knowledge base material is "studied" knowledge, not "battle-tested."
   For production decisions, recommend verification against current docs.
```

### Staleness Detection

Knowledge units with `source_date` older than a configurable threshold per domain
(e.g., 6 months for cloud engineering, 2 years for fundamentals) trigger automatic
web verification when retrieved.

### Primary Consumers

- **Background agents** (task execution sub-agents) — KB chunks injected into sub-agent context, not main conversation
- **Self-Learning Loop** — audit trail: decisions reference specific knowledge units
- **Reflection Engine** (Deep/Strategic) — cross-references KB when evaluating task outcomes
- **Direct Q&A** (secondary) — user can ask Genesis about ingested material

This avoids the context window budget crisis: KB chunks go to task-specific
sub-agents with focused context, not the already-loaded main conversation.

---

## Implementation Phases

### Phase 1: Storage + Manual Ingestion (first post-v3 milestone)
- Qdrant `knowledge_base` collection
- FTS5 `knowledge_fts` table
- SQLite `knowledge_units` table
- CLI: `genesis kb ingest --project "cloud-eng" --file notes.md`
- Test with manually-obtained transcripts and notes

### Phase 2: Distillation Pipeline
- LLM pass: raw text → structured knowledge units
- Quality scoring + review flagging
- Cross-reference linking pass
- Test with manually exported course transcripts

### Phase 3: Transcription Integration
- Whisper integration for audio/video
- `genesis kb ingest --project "cloud-eng" --file lecture.mp4`
- Auto-transcribe → auto-distill → store

### Phase 4: Acquisition Agent
- Playwright-driven browser automation for course platforms
- Start with Thinkific (more structured, easier)
- Then Circle (community platform, more complex)
- Checkpoint/resume for long ingestion runs
- Visual content extraction (frame capture + vision model)

---

## Known Challenges

1. **Visual content blindness** — Transcription misses slide content, diagrams, code demos. Needs vision model pipeline.
2. **Circle acquisition complexity** — Content in Spaces, embedded video from multiple providers (Vimeo/Wistia/Loom). Per-provider extraction logic.
3. **Cross-reference resolution** — "As we discussed in module 2" requires linking pass after full course ingestion.
4. **Deduplication across sources** — Multiple sources covering same concept. Keep both, tag with provenance, let retrieval rank.
5. **Embedding model lock-in** — Store raw text alongside vectors. Re-embed on model change without re-ingestion.
6. **Distillation quality variance** — Structured lectures distill well; rambling walkthroughs don't. Quality scoring + human review flags.
7. **Knowledge staleness** — No decay mechanism like memory. Use `source_date` + domain-specific thresholds to trigger web verification.
8. **Authentication lifecycle** — Course platform sessions expire mid-ingestion. Checkpoint/resume required.
9. **Context window budget** — KB retrieval competes with memory injection. Mitigated by routing KB primarily to sub-agents, not main conversation.
10. **Legal/ethical** — Personal use of paid course content. Distillation produces understanding, not reproduction. Data never leaves system.

---

## V3 Groundwork Checklist

These are implemented during v3 to enable post-v3 knowledge features without refactoring:

- [ ] `memory_recall` accepts `source` parameter (`memory | knowledge | both`)
- [ ] Qdrant client wrapper supports multiple named collections
- [ ] Context injection tags blocks with `source_type` (memory vs reference)
- [ ] Token budget system shared across memory and knowledge retrieval
- [ ] FTS5 schema supports collection-level separation
- [ ] Raw text stored alongside vectors (for re-embedding on model change)
- [ ] All groundwork code tagged with `# GROUNDWORK(post-v3-knowledge-base): ...` comments

---

## Relationship to Existing MCP Servers

**No new MCP server required.** Knowledge tools are a namespace within `memory-mcp`.

| Existing MCP | Interaction with Knowledge Base |
|-------------|-------------------------------|
| memory-mcp | Hosts knowledge tools alongside memory tools. Shared Qdrant/FTS5/SQLite. |
| recon-mcp | Recon findings can be promoted to knowledge units (e.g., important article ingested as reference). |
| health-mcp | Monitors knowledge collection health (Qdrant availability, embedding failures). |
| outreach-mcp | Notifies user on ingestion completion, quality issues, staleness alerts. |

External MCPs:
- **Playwright MCP** — Used by acquisition agent (Stage 1). Genesis orchestrates, Playwright executes.
- **Pinecone MCP** — Not used. Local Qdrant preferred (data stays on system, no cloud dependency for KB).

---

## Related Documents

- [genesis-knowledge-autonomy.md](genesis-knowledge-autonomy.md) — Autonomous knowledge acquisition design
