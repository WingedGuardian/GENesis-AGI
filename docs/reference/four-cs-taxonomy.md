# The Four C's — External Taxonomy

A simpler vocabulary for explaining Genesis to people unfamiliar with
the internal architecture. Maps common AI agent concepts to Genesis's
implementation.

## Origin

Adapted from the "AI Operating System" framework (Context, Connections,
Capabilities, Cadence) popularized by AI engineering communities. The
mapping isn't 1:1, but it provides an accessible entry point.

## The Four C's

### Context (Memory + Knowledge)

**What it means:** Everything the system knows — who you are, what you're
working on, what happened before, what it's learned.

**Genesis implementation:**
- Persistent memory store (10K+ memories, wing/room taxonomy)
- Essential knowledge layer (always-present ~300 token summary)
- Proactive recall (auto-surfaces relevant memories per prompt)
- Knowledge ingestion pipeline (files, URLs, documents → structured knowledge)
- Observation pipeline (pattern detection across sessions)
- User model (synthesized understanding of the user)

**Internal terms:** Wings, rooms, Qdrant, FTS5, essential knowledge,
proactive hook, memory extraction, knowledge units.

### Connections (MCP + Integrations)

**What it means:** How the system interfaces with external tools, services,
and APIs.

**Genesis implementation:**
- 4 MCP servers (health, memory, outreach, recon)
- Telegram relay (bidirectional messaging)
- Dashboard (web UI for status, memory, tasks, settings)
- Gmail integration (email monitoring + triage)
- Browser automation (gstack + CDP)
- GitHub integration (PRs, issues, releases)
- LLM router (multi-provider with circuit breakers)

**Internal terms:** MCP tools, genesis-health, genesis-memory,
genesis-outreach, genesis-recon, bridge, channels.

### Capabilities (Skills + Modules)

**What it means:** What the system can actually do — specialized workflows,
domain expertise, and learned procedures.

**Genesis implementation:**
- Skill library (structured workflows: research, evaluate, develop, etc.)
- Capability modules (domain-specific: crypto, prediction markets, career)
- Procedural memory (learned multi-step patterns, success-tracked)
- Tool registry (known tools available to CC sessions)
- Background session profiles (observe, interact, research)

**Internal terms:** Skills (tier 1/2), superpowers, modules, procedures,
direct sessions, surplus tasks.

### Cadence (Ego + Autonomy)

**What it means:** When and how the system acts on its own — the rhythm
of autonomous behavior.

**Genesis implementation:**
- Ego cycles (adaptive cadence, not fixed intervals)
- Two-ego architecture (User Ego = CEO/strategic, Genesis Ego = COO/operational)
- Surplus compute (opportunistic work during idle periods)
- Reflection hierarchy (micro every 5min, light on-demand, deep weekly, strategic monthly)
- Awareness loop (signal collection ticks)
- Earned autonomy (L0-L4, approval gates, regression)

**Internal terms:** Ego, surplus, reflection, awareness, autonomy levels,
proposals, bulletin board, approval gates.

## When to Use This Taxonomy

- **External docs and README** — Use Four C's as section headers or intro framing
- **Onboarding new contributors** — Start with Four C's, drill into internals as needed
- **Explaining Genesis to non-technical audiences** — Four C's without internal terms
- **Architecture discussions** — Use internal terms (more precise)

## Mapping Table

| Four C's | Genesis Internal | Key Files |
|----------|-----------------|-----------|
| Context | Memory, Knowledge, Observations | `src/genesis/memory/`, `src/genesis/knowledge/` |
| Connections | MCP, Channels, Providers | `src/genesis/mcp/`, `src/genesis/channels/` |
| Capabilities | Skills, Modules, Procedures | `src/genesis/skills/`, `src/genesis/modules/` |
| Cadence | Ego, Surplus, Reflection, Autonomy | `src/genesis/ego/`, `src/genesis/surplus/`, `src/genesis/autonomy/` |
