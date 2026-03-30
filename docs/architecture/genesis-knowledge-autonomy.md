# Genesis Knowledge Autonomy — Concept Design

**Date:** 2026-03-11
**Status:** Concept (informs V3 Phase 8+, V4 features, and post-v3 knowledge pipeline)
**Origin:** User conversation — "I just want to hand it information and talk to it"
**Related docs:**
- `post-v3-knowledge-pipeline.md` — technical storage/ingestion design (knowledge base)
- `genesis-v3-build-phases.md` — Phase 5 (Memory), Phase 8 (Outreach/UI), V4 features
- `genesis-v3-autonomous-behavior-design.md` — master design reference
- `docs/plans/2026-03-09-inbox-monitor-plan.md` — current inbox ingestion

---

## The Principle

> "I don't want to have to worry about the underlying technical infrastructure.
> I just want a capable assistant that I can hand information to, and it can talk
> back to me about it. It'll worry about the details of how that needs to be set
> up for it to use it best. And then it can talk to me about how I want to use it."

This is the difference between Genesis and tools like AnythingLLM or Open WebUI:
those tools move configuration from code to UI (you create workspaces, toggle RAG,
enable web search). Genesis should **eliminate the configuration burden entirely**.
The user hands Genesis information. Genesis decides what it is, where it belongs,
how to store it, how to retrieve it, and how to integrate it with what it already
knows. The user just talks.

---

## What "Hand It Information" Means

The user shouldn't need to think about *how* to give Genesis information. Any
channel, any format, any intent:

| Input Method | Example | Status |
|-------------|---------|--------|
| Drop a file in a folder | PDF, markdown, text file → inbox folder | **Built** (Inbox Monitor, Phase 6) |
| Send a Telegram message | "Check out this article: [URL]" | **Planned** (bridge.py relay, Phase 8) |
| Paste in web UI chat | Raw text, URL, file attachment | **Planned** (CC Chat Widget, Phase 8) |
| Forward an email | Newsletter, receipt, notification | **Not designed** — needs email integration |
| Share from mobile | Screenshot, voice note, photo of whiteboard | **Not designed** — needs mobile channel |
| API call | Programmatic ingestion from other tools | **Not designed** — needs REST endpoint |

### Ingestion Intelligence

When Genesis receives information, it should autonomously:

1. **Classify what it is** — a link to research, a note to remember, a task to
   do, a question to answer, a document to study, a reference to file, raw data
   to analyze. This is LLM judgment, not heuristics.

2. **Decide how to store it** — based on what it is:
   - Ephemeral observation → memory store (activation scoring, may decay)
   - Reference material → knowledge base (immutable, authoritative)
   - Task/action item → task queue (execution pipeline)
   - Question for later → open question in cognitive state
   - Context about the user → user model update

3. **Decide how to process it** — based on what it needs:
   - URL → fetch, extract, summarize, store relevant parts
   - PDF → extract text, distill key concepts, embed
   - Raw text → classify intent, store appropriately
   - Image → describe (vision model), extract text (OCR), store
   - Audio/video → transcribe, distill, store
   - Code → analyze, store as reference or create task

4. **Connect it** — link new information to existing knowledge:
   - "This article about X relates to the project you asked about last week"
   - "This contradicts something in your knowledge base — flagging for review"
   - "This is an update to information I already have — merging"

5. **Report back** — tell the user what it did, concisely:
   - "Got it. Stored as a reference on [topic]. Connected to your [project] notes."
   - "Interesting — this contradicts what [source] says about [concept]. Want me
     to dig into which is current?"
   - "Saved. By the way, this is the third article you've sent me on [topic] this
     week. Want me to put together a synthesis?"

---

## What "Talk Back About It" Means

### Retrieval Intelligence (Confidence-Gated)

When the user asks a question, Genesis should transparently navigate its
knowledge sources without the user ever toggling a switch or choosing a mode:

```
User asks a question
        │
        ▼
┌─────────────────────┐
│ 1. Query local       │  ← Memory store + Knowledge base
│    knowledge sources │     (Phase 5 hybrid retriever)
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 2. Score retrieval   │  ← Are results relevant? Sufficient?
│    confidence        │     (NEW: confidence threshold layer)
└─────────┬───────────┘
          │
    ┌─────┴──────┐
    │            │
 HIGH          LOW
    │            │
    ▼            ▼
┌────────┐  ┌─────────────────┐
│ Answer │  │ 3. Web search    │  ← Fill gaps from live sources
│ from   │  │    + synthesis   │     (ephemeral, not persisted
│ local  │  │                  │      unless explicitly learned)
└────────┘  └────────┬────────┘
                     │
                     ▼
              ┌─────────────┐
              │ 4. Blended   │  ← "Here's what I know, plus
              │    answer    │     what I just found online"
              └─────────────┘
```

**Key design decisions:**
- Web results are **ephemeral context** by default — injected into the current
  response but NOT persisted to memory/knowledge unless Genesis judges them worth
  keeping or the user says "remember this."
- The confidence threshold is LLM-judged, not a numeric cutoff. The question is:
  "Do I have enough information to answer this well, or am I guessing?"
- Genesis should be **transparent** about its source: "Based on your [source]
  notes..." vs. "I searched the web and found..." vs. "From what I know..."
- Authority hierarchy from `post-v3-knowledge-pipeline.md` applies:
  Knowledge base (studied) > Genesis training (model weights) > Web (live).

### Proactive Knowledge Integration

Genesis shouldn't just answer questions — it should **actively integrate**
new information with existing knowledge:

- After ingesting a document, notice it relates to something the user asked
  about before → proactively offer the connection
- During deep reflection, identify knowledge gaps (topics the user cares about
  where Genesis has thin coverage) → suggest research during surplus compute
- When web search reveals information that updates or contradicts stored
  knowledge → flag for review, don't silently update
- When multiple sources on the same topic accumulate → offer synthesis

This is the V4 "anticipatory intelligence" seed — it starts as simple
cross-referencing in V3 and grows into genuine anticipation in V5.

---

## How This Maps to Existing Architecture

### Already Built (V3 Phases 0-6)

| Capability | Component | Gap |
|-----------|-----------|-----|
| Memory storage & retrieval | Phase 5 MemoryStore, HybridRetriever | No confidence gating on retrieval |
| Document ingestion from filesystem | Inbox Monitor (Phase 6) | Single channel (filesystem only) |
| LLM classification of inputs | Inbox Monitor classifier | Only classifies inbox items, not arbitrary inputs |
| Observation storage | Phase 0 CRUD, Phase 4 ResultWriter | Works, just needs more input channels |
| Memory linking | Phase 5 MemoryLinker | Automatic, needs testing at scale |
| Context assembly | Phase 4 ContextAssembler | Depth-scoped but no knowledge base source |

### Phase 8 Hooks (V3)

Phase 8 is where several knowledge autonomy pieces naturally land:

1. **Multi-channel ingestion** — The CC Chat Widget and Telegram relay (both
   Phase 8) create new input channels. Each should route through the same
   classification pipeline as the inbox monitor, not build separate classifiers.
   → **Recommendation:** Extract inbox monitor's classification logic into a
   shared `IngestClassifier` that all channels use.

2. **Morning report knowledge integration** — The morning report already reads
   cognitive state. It should also surface: "You sent me 3 things about [topic]
   this week — want a synthesis?" or "I noticed a gap in my knowledge about
   [area you've been asking about] — should I research it?"

3. **Dashboard knowledge view** — The approval dashboard should show Genesis's
   knowledge state: what topics it knows well, what's thin, what's stale. Not
   a file browser — a **competence map**. "I'm strong on [X], moderate on [Y],
   weak on [Z]."

### V4 Hooks

1. **Proactive inbox research** (already in V4 scope fence) — Genesis-initiated
   research based on user's notes. The knowledge autonomy principle gives this
   a clearer frame: Genesis notices gaps in its knowledge about topics the user
   cares about and fills them proactively.

2. **Channel learning** (V4) — Applies to ingestion channels too. Genesis
   learns: "User tends to send research links via Telegram and detailed docs
   via inbox folder." Adapts its classification and processing accordingly.

3. **Anticipatory intelligence** (V4→V5) — The "third article on [topic] this
   week" pattern. Genesis notices interest patterns and proactively deepens
   its knowledge in those areas without being asked.

### Knowledge Pipeline Integration

`post-v3-knowledge-pipeline.md` describes the technical infrastructure for a
knowledge base (separate Qdrant collection, distillation pipeline, authority
hierarchy). Knowledge autonomy is the **behavioral layer on top of that
infrastructure**:

- Knowledge pipeline = HOW information is stored and retrieved
- Knowledge autonomy = WHEN and WHY Genesis decides to store, retrieve, connect,
  or proactively research

The pipeline doc's implementation phases (manual ingestion → distillation →
transcription → acquisition agent) should be reframed through the autonomy lens:
Genesis shouldn't need the user to run `genesis kb ingest --file notes.md`.
Genesis should see the file, classify it as reference material, and ingest it
autonomously — then tell the user what it did.

---

## Design Constraints

1. **Privacy first** — All knowledge stays local (Qdrant on localhost, SQLite on
   disk). No cloud vector DBs. No external indexing services. The user's
   knowledge is the user's knowledge.

2. **Transparent operations** — Genesis must always be willing to say: "Here's
   what I stored, here's how I classified it, here's what I connected it to."
   No black-box knowledge management. Aligns with CAPS markdown convention.

3. **User authority** — Genesis proposes knowledge organization; the user can
   override. "I filed this under [topic]" + "Actually, that's about [other topic]"
   → Genesis learns the correction.

4. **No silent updates** — Genesis does not silently modify stored knowledge. If
   new information contradicts existing knowledge, it flags the conflict. The
   user (or a governed autonomous process) decides which is authoritative.

5. **Graceful degradation** — If Qdrant is down, Genesis still has FTS5. If
   embedding fails, Genesis still stores raw text. If web search fails, Genesis
   answers from what it has and says so. No hard failures on knowledge operations.

---

## Anti-Patterns (What Genesis Should NOT Do)

- **Don't make the user create workspaces** — Genesis organizes by topic/domain
  automatically, not through user-defined containers
- **Don't make the user toggle RAG on/off** — Retrieval happens automatically
  when relevant; the user shouldn't know or care
- **Don't make the user choose "web search" vs "local knowledge"** — Genesis
  decides based on confidence, transparently
- **Don't dump everything into one flat store** — But also don't require the user
  to define the taxonomy. Genesis proposes organization, learns from corrections.
- **Don't treat all information equally** — Studied reference material (knowledge
  base) has higher authority than casual observations (memory). A forwarded
  article has different weight than a pasted excerpt from an official spec.

---

## Open Questions

1. **Knowledge base vs memory boundary** — When does an ingested document become
   "knowledge" (immutable, authoritative) vs "memory" (episodic, decayable)?
   Current design: the user decides (or Genesis proposes and user confirms).
   Could Genesis learn this distinction over time?
   *V3 resolution:* Separate Qdrant collections (`episodic_memory` vs `knowledge_base`)
   with explicit scope tagging ("internal" vs "external"). User-initiated ingestion
   determines placement. Autonomous learning deferred to V4.

2. **Multi-user** — V3 is single-user. If Genesis ever serves multiple users,
   knowledge authority becomes complex (whose knowledge takes precedence?).
   Not a V3/V4 concern, but worth noting.
   *V3 resolution:* Out of scope. Single-user assumption hardcoded throughout.

3. **Knowledge staleness at scale** — `post-v3-knowledge-pipeline.md` has
   domain-specific staleness thresholds. At scale (hundreds of knowledge units),
   staleness checking on every retrieval becomes expensive. Batch staleness
   review during deep reflection? Or lazy staleness check only when retrieved?
   *V3 resolution:* Deferred. Knowledge base is currently empty in V3 — staleness
   at scale is a post-V3 concern. Design favors lazy retrieval-time checks with
   deep reflection as a secondary sweep.

4. **Synthesis quality** — "Put together a synthesis of these 3 articles" is
   harder than it sounds. Requires understanding what the user cares about in
   each source, not just summarizing. User model integration is critical here.
   *V3 resolution:* Deferred to V4 meta-prompting. V3 inbox monitor evaluates
   individual articles but does not synthesize across sources.

---

## Implementation Sequence

This is not a new phase — it's a lens that applies across existing phases:

| When | What | Where to Build |
|------|------|---------------|
| **Phase 8** (V3) | Shared IngestClassifier (extract from inbox monitor) | `genesis.ingestion` package |
| **Phase 8** (V3) | Multi-channel ingestion (Telegram, web UI → same pipeline) | bridge.py, chat widget |
| **Phase 8** (V3) | Retrieval confidence gating (LLM judges sufficiency) | Phase 5 HybridRetriever extension |
| **Phase 8** (V3) | Web search as automatic fallback when confidence low | Router + retriever integration |
| **Phase 8** (V3) | "Here's what I did with it" feedback after ingestion | Outreach pipeline |
| **Post-V3** | Knowledge base storage + distillation pipeline | `post-v3-knowledge-pipeline.md` |
| **V4** | Proactive knowledge gap detection + research | Surplus + deep reflection |
| **V4** | Interest pattern detection ("3rd article on [topic]") | User model + learning |
| **V4** | Channel-aware ingestion learning | Channel learning feature |
| **V5** | Anticipatory knowledge acquisition | Full anticipatory intelligence |

---

## Related Documents

- [post-v3-knowledge-pipeline.md](post-v3-knowledge-pipeline.md) — Knowledge ingestion pipeline design
