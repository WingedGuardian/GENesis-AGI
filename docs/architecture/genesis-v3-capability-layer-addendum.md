# Genesis v3: Capability Layer Addendum — "Single Pane of Glass"

**Date:** 2026-02-28
**Status:** Design addendum to v3 architecture
**Decision source:** Brainstorming session between user and Claude Code, 2026-02-28
**Depends on:** `genesis-v3-build-phases.md` (Phases 0-9), `genesis-v3-autonomous-behavior-design.md`

---

## What This Document Is

An architectural addendum to the Genesis v3 design that describes the **Capability Layer** —
the user-facing tools and self-extension mechanism that transform Genesis from "autonomous
cognitive system" into "single pane of glass that replaces 90% of other AI tools."

This document does NOT modify any v3 phase. It describes:
1. Two small groundwork additions to Phase 0 and Phase 6
2. A post-Phase-9 Capability Layer built on top of the cognitive foundation
3. The strategic identity that guides capability decisions

---

## Strategic Identity: Hybrid Orchestrator

Genesis is NOT trying to replicate every purpose-built AI tool. It cannot beat NotebookLM's
audio synthesis, Gemini's 2M context window, or Cursor's real-time IDE integration — each
backed by billions in engineering.

**Genesis's competitive moat is the combination of:**

1. **Memory that compounds across ALL tasks** — Every other AI tool forgets you between
   sessions. Genesis remembers everything: your style, your preferences, what worked,
   what didn't, what you're working on, what you care about.

2. **Model routing** — Use the best model for each sub-task (Opus for judgment, Sonnet for
   code, Gemini for long context, local models for free). No single-vendor tool does this.

3. **Tool orchestration** — "Research X, then build Y based on what you found, then write
   a report about it" as one workflow, not three separate tools.

4. **Proactive intelligence** — Surfaces things you didn't ask for. No other tool does this.

5. **Self-extension** — When Genesis encounters a task it can't handle, it proposes a new
   tool to fill the gap. The system grows its own capability over time.

**The architectural principle:** Genesis doesn't need to BUILD every capability. It needs to
be the brain that USES the right capability for each job, remembers across all of them, and
learns from every interaction. New capabilities = new tools integrated, not new features
coded from scratch.

```
Layer 1: CORE (always on, built-in — V3 Phases 0-9)
├── Brain (Claude Code + Genesis cognitive layer)  # Updated 2026-03-07
├── Infrastructure (Agent Zero — dashboard, relay, health, signals)
├── Memory (persists across everything)
├── Model routing (best model for each sub-task)
└── Reflection (learns from everything it does)

Layer 2: POWER TOOLS (invoked as tools, not rebuilt)
├── Code work → Claude Code (native)  # OpenCode deferred to V5
├── Browser → Playwright MCP
├── Web research → Search + browse + synthesize
└── File system → read, write, manage

Layer 3: EXTENDED CAPABILITIES (MCP tool dispatch)
├── Document generation (reports, presentations, diagrams)
├── Data analysis (pandas, visualization)
├── Communications (email, calendar, messaging)
├── Media (transcription, image gen, video analysis)
└── Integrations (GitHub, Linear, Notion, LinkedIn, etc.)

Layer 4: SELF-EXTENSION (the meta-capability)
└── Genesis uses Layer 2 (coding) to BUILD new Layer 3 tools
    when it encounters a task it can't currently handle
```

---

## V3 Groundwork Additions

Two small additions to existing v3 phases. Neither modifies phase scope or ordering.

### Phase 0 Addition: Tool Registry Table

```sql
CREATE TABLE tool_registry (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,              -- e.g. "deep_research", "code_edit", "linkedin_writer"
    category TEXT NOT NULL,          -- "research", "coding", "automation", "generation", "integration"
    description TEXT NOT NULL,       -- what it does (used by LLM for tool selection)
    tool_type TEXT NOT NULL,         -- "builtin", "mcp", "script", "workflow", "proposed"
    provider TEXT,                   -- which MCP server or tool provides it
    cost_tier TEXT,                  -- "free", "cheap", "moderate", "expensive"
    success_rate REAL,              -- tracked over time, starts NULL
    invocation_count INTEGER DEFAULT 0,
    last_used TEXT,
    enabled BOOLEAN DEFAULT 1,
    created_at TEXT NOT NULL,
    metadata TEXT                    -- JSON: model requirements, input/output schemas, constraints
);
```

**Why:** Without a registry, Genesis relies on prompt context to know what tools exist.
That's fragile — tools get lost in long prompts, new tools aren't discoverable, and
there's no tracking of what works. The registry makes capabilities a first-class data
structure that the LLM can query.

**How it's used:**
- Task decomposition queries the registry: "what tools can handle research tasks?"
- Self-extension checks the registry: "do I already have a tool for this?"
- Learning updates the registry: `success_rate` and `invocation_count` after each use
- User can query: "what can you do?" → lists enabled tools by category

### Phase 6 Addition: Tool Proposal Output

Expand capability gap tracking (already in Phase 6) to produce **tool proposals** when a
gap is detected repeatedly:

```
Capability gap detected (existing Phase 6 behavior):
  description: "User asked me to write LinkedIn posts in their voice"
  frequency: 3
  first_seen: "2026-04-10"
  last_seen: "2026-04-15"
  feasibility: "high"

NEW — Tool proposal generated:
  name: "linkedin_voice_writer"
  category: "generation"
  description: "Write LinkedIn posts in user's voice using style profile from procedural memory"
  implementation_sketch: "Workflow tool: load voice profile → pre-writing discussion →
    draft with style constraints → edit tracking → learn from changes"
  estimated_effort: "medium"
  dependencies: ["procedural_memory", "user_model"]
  status: "proposed"  -- awaits user approval
```

**Governance:** Propose-only. Genesis designs the tool and presents it to the user. User
approves before any code is written. This does NOT require V5 autonomy levels — it's a
structured suggestion, not autonomous action.

**Self-extension flow:**
1. Phase 6 detects recurring capability gap
2. Deep Reflection (Phase 7) reviews gaps, generates tool proposal
3. Proposal stored in `tool_registry` with `tool_type = "proposed"`
4. Next outreach cycle (Phase 8) or direct conversation surfaces the proposal to user
5. User approves → Genesis uses Claude Code to build the tool
6. Tool registered as `tool_type = "workflow"` or `"script"`, enabled
7. Learning loop tracks `success_rate` from usage

---

## Post-Phase-9: Capability Layer

Built after the cognitive foundation (Phases 0-9) is verified. Ordered by user value.

### Cap-1: User-Defined Workflow Automation

**What:** Users define recurring tasks that Genesis executes on a schedule or trigger.

**Why first:** This is the primary use case for Genesis as a "single pane of glass."
"Every morning, check X, summarize Y, send me Z." The cognitive layer already has scheduling
(Surplus Infrastructure, Phase 3). User-defined automations extend it.

**How it works:**
- User describes a workflow conversationally: "Every Monday, check my LinkedIn analytics,
  compare to last week, and tell me what's working."
- Genesis decomposes into: trigger (cron: Monday 8AM) → steps (API call → analysis →
  summary) → output (outreach via WhatsApp)
- Stored as a registered workflow in `tool_registry`
- Executed by the surplus/scheduling infrastructure (Phase 3)
- Results tracked, user can adjust or disable

**Implementation:** Extension of Phase 3's surplus scheduling + Phase 8's outreach pipeline.
The scheduling infrastructure already exists for system tasks. User-defined automations are
the same mechanism with user-provided task definitions.

### Cap-2: Research Workflow

**What:** Structured multi-step research: define question → gather sources → cross-reference →
synthesize → produce report. What Gemini Deep Research and Perplexity do, but with Genesis's
memory and learning.

**Why it matters:** Research is a primary reason users switch to other AI tools. "Go to Gemini
for the long context" or "Go to Perplexity for the sources." Genesis should handle this
natively.

**How it works:**
- User asks a research question
- Genesis decomposes: search strategy → source gathering (browser tool) → source evaluation
  → cross-referencing → synthesis → report generation
- Sources cited with links (not hallucinated)
- Report stored in memory for future reference
- Research quality improves over time (Learning Loop tracks which research approaches
  the user found useful)

**Model routing advantage:** Genesis can route the search step to a model with web access,
the analysis step to a model with strong reasoning, and the synthesis step to a model that
writes well. No single-vendor tool can do this.

### Cap-3: Artifact Generation

**What:** Produce structured outputs beyond chat text — reports, documents, presentations,
code projects, diagrams.

**Why it matters:** "Single pane of glass" means Genesis produces things you can USE, not
just things you can READ in a chat window.

**How it works:**
- Genesis uses coding tools (Claude Code) to generate files
- For documents: Markdown → PDF pipeline, or direct file generation
- For presentations: python-pptx or similar libraries
- For diagrams: Mermaid, graphviz, or image generation
- For code projects: Full project scaffolding via Claude Code
- Artifacts stored, versioned, retrievable

**Key principle:** Genesis doesn't need a built-in presentation engine. It needs to know
HOW to use presentation tools. If it doesn't have python-pptx, it proposes installing it
(self-extension). The tool registry tracks which artifact types Genesis can produce.

### Cap-4: Voice/Style Learning (Canonical Example)

**What:** Genesis learns the user's writing/communication style and can produce content
that sounds like the user, not like AI.

**Why it's canonical:** This showcases exactly why Genesis's memory + learning system is
the competitive moat against every other AI tool. Every competitor (Jasper, HyperWrite,
StoryChief, Copy.ai) does static style capture: paste samples → extract patterns → generate.
Genesis does dynamic style learning that compounds over time.

**How it works:**

1. **Initial calibration:**
   - User provides 5-10 writing samples (LinkedIn posts, emails, documents)
   - Genesis extracts style markers into procedural memory:
     sentence length distributions, vocabulary preferences, tone markers,
     structural patterns, recurring phrases, what the user avoids

2. **Conversational refinement (the differentiator):**
   - User describes what they want to say (voice or text)
   - Genesis extracts the core message and asks sharpening questions:
     "You mentioned X but didn't connect it to Y — expand or drop?"
   - Together they refine the message BEFORE any generation happens
   - This is collaboration, not generation

3. **Style-constrained generation:**
   - Genesis writes a draft with the voice profile injected as constraints
   - The procedural memory rules shape word choice, structure, tone
   - The user's recent context (from memory) informs topical relevance

4. **Edit-driven learning:**
   - User reviews and edits the draft
   - Every edit becomes a style correction in the Self-Learning Loop:
     "User changed 'leverage synergies' to 'make it work together'"
     → procedural rule: "avoid corporate buzzwords, use plain language"
   - Edits classified: style correction vs. content correction vs. factual fix
   - Only style corrections update the voice profile

5. **Compounding quality:**
   - Day 1: Drafts need heavy editing (confidence: 0.3)
   - Day 30: After ~15 posts, drafts need moderate editing (confidence: 0.6)
   - Day 90: After ~40 posts, drafts are near-final (confidence: 0.8+)

**Procedural memory example:**
```
Procedure: "user_voice_linkedin"
Confidence: 0.73
Invocations: 14
Success rate: 0.71
Rules:
  - 1st person, conversational but authoritative
  - Opens with hook question or bold statement; NEVER "I'm excited to share..."
  - Avoids: buzzwords, bullet lists, emoji overload, hedging ("I think", "maybe")
  - Paragraphs: 2-3 sentences max, generous white space
  - Always includes a concrete example or personal story
  - Closes with engagement question, not a CTA
  - Write with conviction — user consistently removes hedging language
```

**Why no other tool can do this:**

| Feature | Jasper/HyperWrite/Copy.ai | Genesis |
|---------|---------------------------|---------|
| Style capture | Paste samples once | Samples + continuous learning from edits |
| Learning | Static snapshot | Every edit refines procedural memory |
| Refinement | Generate → you edit | Discuss → co-create → generate → you edit → it learns |
| Memory | Resets per session | Remembers every post, edit, preference |
| Context | Just "your voice" | Voice + goals + last week's post + industry context (recon) |

---

## Interface: Chat-First, Multi-Channel

Genesis is accessed through conversation. The chat IS the interface.

**Day-1 channels:**
- **WhatsApp** — Mobile, quick tasks, outreach delivery (already in v2)
- **Telegram** — Mobile/desktop, richer formatting (already planned for v3)
- **Agent Zero Web UI** — Desktop, rich output rendering (code, charts, files, artifacts)

**Future channels (extensible by design):**
- Email (both inbound tasks and outbound delivery)
- Slack / Discord
- API (for programmatic access)
- Voice interface (transcription → Genesis → TTS response)

**Interface principle:** Genesis doesn't need different UIs for different task types. A
research task, a coding task, a writing task, and an automation setup all happen through
conversation. The web UI renders rich outputs (code blocks, file previews, charts) that
chat can't, but the interaction model is always conversational.

**Implementation reference — CoPAW channel architecture:** Alibaba's
[CoPAW](https://copaw.bot/) (open-source personal agent workstation) implements channels
as a plugin registry with CLI management (`list, install, remove, configure`). Each channel
is an independently deployable adapter. CoPAW also uses a built-in message queue across
channels to ensure no dropped messages under multi-channel load. Worth evaluating this
pattern when Genesis scales beyond 2-3 channels — the current ad-hoc approach is fine for
V3's WhatsApp + Telegram + Web UI, but a channel registry becomes valuable at 5+ channels.
CoPAW's heartbeat mechanism for proactive autonomous tasks validates Genesis's Cognitive
Surplus design (Phase 3) — same concept, different name.

---

## The "90% Replacement" Map

| AI Tool | What You Use It For | How Genesis Replaces It |
|---------|--------------------|-----------------------|
| **Claude Code** | Coding, refactoring, debugging | Claude Code as a tool (already in v3 design) |
| **Gemini** | Long context analysis, research | Model routing to Gemini for long-context tasks + research workflow |
| **NotebookLM** | Course/media analysis, source conversations | Knowledge base (post-v3) + conversational recall over ingested material |
| **Perplexity** | Research with sources | Research workflow (Cap-2) with source citation |
| **ChatGPT** | General Q&A, brainstorming | Agent Zero conversation + memory makes it better over time |
| **Cursor/OpenClaw** | IDE-integrated coding | Claude Code / OpenCode as tools + code context from memory |
| **Jasper/Copy.ai** | Brand voice content | Voice/style learning (Cap-4) via procedural memory |
| **Zapier/Make** | Workflow automation | User-defined workflow automation (Cap-1) |
| **Notion AI** | Document generation, knowledge management | Artifact generation (Cap-3) + knowledge base |

**The 10% it doesn't replace (initially):** Highly specialized tools with proprietary
capabilities (image generation models, video editing, 3D rendering, real-time collaborative
editing). These become Layer 3 integrations over time via the self-extension pipeline.

---

## Relationship to Existing V3 Documents

This addendum sits alongside and builds on:

1. **genesis-v3-vision.md** — Identity: "cognitive partner that grows with your user."
   The Capability Layer is HOW it grows — by extending its own toolkit.
2. **genesis-v3-build-phases.md** — Build order. Capability Layer is post-Phase-9.
   Two small additions to Phase 0 (`tool_registry` table) and Phase 6 (tool proposals).
3. **genesis-v3-autonomous-behavior-design.md** — The cognitive layer that makes
   capabilities intelligent. Memory makes research smarter. Learning makes voice
   capture improve. Reflection makes tool selection better.
4. **genesis-v3-gap-assessment.md** — Addresses Q1 ("What does Genesis do in Week 1?")
   by defining day-1 capabilities beyond the cognitive layer.
5. **post-v3-knowledge-pipeline.md** — Knowledge base is a Layer 3 capability that
   enables the NotebookLM replacement use case.

---

## Task Status Board — User-Facing Blocker Coordination

Genesis needs a way to surface what it's working on, what it's stuck on, and where
user input would unblock progress. This is the **Task Status Board** — a lightweight
UI concept from v2 that becomes essential in v3's autonomous context.

**Why it matters:** As Genesis works autonomously (background tasks, workaround
searches, multi-step workflows), the user loses visibility. Without a status board,
the user doesn't know that Genesis has been stuck on a blocker for 20 minutes when
a 10-second human answer would have unblocked it.

**What the board shows:**
- **Active tasks** — what Genesis is currently working on, with progress indication
- **Blocked tasks** — what's stuck, why, and what would unblock it
- **Workaround attempts in progress** — which workarounds have been tried, which
  are currently being explored, and what the budget ceiling is
- **Awaiting user input** — explicit "I need you for this" items, prioritized
- **Recently completed** — what finished since the user last checked

**Interface:** Rendered in the Agent Zero Web UI as a persistent sidebar or
dashboard view. Chat channels (WhatsApp, Telegram) get a summarized version on
request ("what are you working on?") or when a blocker has been waiting too long
(configurable threshold, default 15 minutes).

**Implementation:** Built on execution_traces + a lightweight task_status table or
in-memory state. The board is a read view over existing data, not a separate system.
The outreach pipeline (Phase 8) handles "blocker waited too long" notifications.

**V2 reference:** This was the "task status" UI board envisioned in v2. V3 has the
data infrastructure to make it real — execution traces track task state, the
workaround search protocol tracks what's been tried, and the outreach pipeline
handles notification delivery.

---

## Open Questions

1. **Tool registry bootstrapping:** What tools ship pre-registered in the registry on
   day 1? Need a concrete list of built-in tools with their registry entries.

2. **Workflow definition UX:** How does a user define an automated workflow through
   conversation? What's the confirmation step before Genesis schedules it? How does
   the user edit/disable/delete existing automations?

3. **Artifact storage:** Where do generated files live? Agent Zero's file system? A
   dedicated artifacts directory? How are they versioned and retrievable?

4. **Research source quality:** How does Genesis evaluate source reliability? Does it
   learn which sources the user trusts over time?

5. **Voice profile portability:** Can the user export their voice profile for use
   elsewhere? Is it human-readable (procedural rules) or opaque (embeddings)?
   Current design uses procedural memory (human-readable rules), which is a strength.

6. **Self-extension scope:** What's the maximum complexity of a self-proposed tool?
   Simple scripts and API wrappers are safe. Full MCP servers are complex. Where's
   the line for propose-only vs. "this needs a dedicated design session"?

7. **Task status board scope:** What's the right granularity for the status board?
   Every sub-agent step? Only top-level tasks? How much detail overwhelms vs.
   how little leaves the user blind? Likely needs user-configurable verbosity.

---

## Related Documents

- [genesis-v3-autonomous-behavior-design.md](genesis-v3-autonomous-behavior-design.md) — Primary behavior architecture
- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Implementation order
