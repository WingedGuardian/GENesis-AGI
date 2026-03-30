# Post-V5 Horizon — Future Capabilities & Migrations

**Status:** TRACKING — items collected during research for eventual consideration.
Not designed, not scoped. Reviewed when V5 planning begins or when the
technology landscape changes materially.

**Purpose:** Capture ideas that are too early to build but too valuable to forget.
Each item includes the trigger condition that would move it from "tracking" to
"designing."

---

## 1. Embedding Model Migration

**Current state:** qwen3-embedding:0.6b on Ollama (local, free, 1024 dims).
Mistral Embed as cloud fallback. Text-only.

**Target state:** A higher-quality embedding model — ideally local, ideally
multimodal — that replaces qwen3 as the primary. Full Qdrant re-index at
migration time. Clean cutover, not dual-provider.

### Why not now

- **Dimensions**: 1024 is the sweet spot for Genesis's content (English
  conversational context, procedures, reflections). Research confirms the
  accuracy curve flattens between 768–1024 for most content. Going to 3072
  triples storage and slows HNSW traversal with marginal quality gains.
  Genesis doesn't need to distinguish "voltage tolerance threshold" from
  "voltage threshold tolerance."
- **Privacy**: Cloud embedding APIs (Gemini Embedding 2, OpenAI) send all
  memory content to third parties. Unacceptable for Genesis's memory corpus.
- **Cost**: Cloud embedding adds per-request cost to every memory
  store/retrieval operation. Conflicts with quality-over-cost philosophy
  (cost should be observable, not a tax on every operation).
- **Maturity**: Frontier multimodal embedding models (Gemini Embedding 2,
  Jina v4) are in preview or have restrictive licenses (Jina CC-BY-NC-4.0).

### Candidates to monitor

| Model | Dims | Modalities | Local? | License | Notes |
|-------|------|-----------|--------|---------|-------|
| Gemini Embedding 2 | 3072 (MRL to 768) | Text, image, video, audio, PDF | No (API) | Proprietary | Benchmark leader. Privacy concern. |
| Jina Embeddings v4 | 2048 (MRL to 128) | Text, image, visual docs | Potentially (Qwen2.5-VL-3B base) | CC-BY-NC-4.0 | Commercial use restricted. |
| BGE-M3 | 1024 | Text (multi-granularity, multilingual) | Yes | MIT | Strong open-source option. Text-only. |
| Future Ollama models | TBD | TBD | Yes | TBD | Monitor Ollama model releases. |

### Migration trigger

Move from "tracking" to "designing" when ANY of these conditions are met:
1. A local multimodal embedding model with ~1024 dims, permissive license,
   and quality competitive with Gemini Embedding 2 becomes available on Ollama.
2. Genesis gains multimodal capabilities (image/audio processing) that make
   multimodal embeddings valuable rather than theoretical.
3. Retrieval quality becomes a measurable bottleneck in production memory ops.

### Migration approach (when triggered)

- Full re-index: run all existing memories through new model, rebuild Qdrant
  collection with new dimensions. The `EmbeddingProvider` already supports
  primary/fallback pattern — swap providers, re-embed, cutover.
- Consider Matryoshka: store at full dims, search at reduced dims, re-rank
  at full resolution. Only if the quality/speed tradeoff justifies the
  complexity.

---

## 2. Browser Automation with Live View & Takeover

**Current state:** Playwright via MCP server. Process-level isolation. No
live view, no human intervention capability.

**Target state:** Browser automation with optional live view (watch Genesis
navigate in real-time) and human takeover (pause agent, take manual control,
resume).

### Why not now

- Current Playwright setup is functional and free.
- Genesis is single-agent in a trusted container — enterprise isolation
  features (Firecracker microVMs) are unnecessary overhead.
- Live view requires a streaming UI component that doesn't exist yet.

### Design inspiration

AWS AgentCore Browser Tool provides:
- Firecracker microVM isolation per session
- Live view with human intervention capability
- Integration with Playwright (not a replacement — a wrapper)

### Migration trigger

Move to "designing" when:
1. Genesis dispatches browser tasks autonomously (not just via CC MCP) and
   the user wants visibility into what it's doing.
2. A multi-agent architecture requires browser session isolation.
3. The dashboard (neural monitor) evolves to support real-time streaming views.

### Implementation sketch

- Build a WebSocket-based live view into the neural monitor dashboard.
- Playwright already captures screenshots — stream them at ~2fps.
- Add a "pause/resume" control that suspends the agent's browser actions.
- No need for microVM isolation unless multi-agent.

---

## 3. Canvas / Visual Workspace Dashboard

**Current state:** Neural monitor dashboard shows health metrics, circuit
breaker states, cost tracking, error logs.

**Target state:** A richer workspace view where active tasks, research findings,
reflections, and outreach items are visual cards the user can rearrange,
annotate, and dismiss. Inspired by Replit Agent 4's Canvas concept.

### Why not now

- The neural monitor serves its current purpose (operational health).
- The UX work is substantial and not on the critical path.
- Genesis's primary interaction channel is conversation, not dashboard.

### Design inspiration

Replit Agent 4 Canvas:
- Infinite scratchpad with cards for different work products
- Visual arrangement and annotation
- Real-time collaboration
- Progress visibility across parallel tasks

### What to steal

- Card-based representation of Genesis's working state (tasks, research,
  reflections as cards).
- Progress indicators for parallel CC sessions (currently opaque to user).
- User annotation/dismissal of items (feedback loop for priority).

### Migration trigger

1. Dashboard becomes a primary interaction surface (V4+).
2. User feedback indicates need for better visibility into Genesis's state.

---

*Document created: 2026-03-14*
*Status: Tracking — reviewed during version planning milestones*
