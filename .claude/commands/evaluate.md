---
name: evaluate
description: >
  Evaluate technologies, tools, articles, videos, and competitive developments
  against Genesis architecture. Invoke when reviewing external sources,
  conducting competitive analysis, evaluating tools for adoption, or during
  strategic reflection on industry developments.
---

# Technology & Intelligence Evaluation Framework

## Purpose

Conduct rigorous, balanced evaluation of external technologies, tools, articles,
and competitive developments against Genesis's architecture, design philosophy,
and build roadmap. Produce actionable findings — not summaries.

## Core Principle

**Give the full picture.** Do not undersell, do not be sycophantic. Think about
how it could help, how it can't, and how maybe it /could/. Every finding gets
the four-lens treatment before any conclusions are drawn.

## Phase 1: Source Acquisition

### Parallel Research
Fetch all sources simultaneously. Never serialize independent lookups.

### Obstacle Exhaustion
When a source is inaccessible, exhaust all autonomous options before involving
the user:

1. Try the primary tool (WebFetch, scrape, direct access)
2. Try alternative tools in the toolkit (Firecrawl, other MCP tools)
3. Route to a different model/service that CAN access the content type:
   - YouTube video → Gemini API (native YouTube URL support)
   - Paywalled article → Firecrawl (JS rendering, paywall bypass)
   - Authenticated service → check for specialized MCP tools
4. Try creative workarounds (transcript APIs, metadata services, cached versions)
5. Only then ask the user — with specific options, not "what was it about?"

### Proactive Capability Surfacing
Even when a workaround succeeds, note the faster/better path for later. Example:
"Resolved via Gemini, but authorizing Firecrawl would handle this and more."

## Phase 2: Four-Lens Evaluation

Evaluate EVERY finding through all four lenses before drawing conclusions.
Do not skip lenses or collapse them.

### Lens 1: How It Helps
- Direct applicability to Genesis architecture, current phase, or planned phases
- Ready-to-use tools, libraries, or integrations
- Validated patterns that confirm our design decisions

### Lens 2: How It Doesn't Help
- Platform incompatibilities (OS, runtime, deployment model)
- Architectural misalignment with Genesis design philosophy
- Scope mismatch (solves a problem we don't have)
- Maturity or reliability concerns

### Lens 3: How It COULD Help
- Patterns worth stealing even if the tool itself isn't usable
- UX concepts applicable to our dashboard/interface
- Architectural ideas for future versions (V4/V5)
- Creative applications the original creators didn't intend
- Think beyond the obvious — web UI potential, cross-tool composition,
  indirect value through integration with other Genesis components

### Lens 4: What to Learn From It
- Distinguish "use this tool" from "learn from this approach"
- Engineering patterns (efficiency, scaffolding, orchestration)
- Competitive positioning — where we're genuinely ahead AND behind
- Design principles that transcend the specific implementation

## Phase 3: Architecture Mapping

For each finding, explicitly assess:

### Competitive Position
| Dimension | Them | Us | Honest Assessment |
|-----------|------|-----|-------------------|

Be specific about where we're ahead, behind, and different. "Different" is
not a euphemism for behind — sometimes a different approach is genuinely better
for our use case.

### Architecture Impact
Classify each finding:
- **Validates** our existing design (confidence boost, no action needed)
- **Extends** our design (compatible addition, queue for appropriate phase)
- **Challenges** our design (rethink needed, discuss before acting)
- **Irrelevant** to our design (note and move on)

### Scope Tag
Every actionable item gets a version tag:
- **V3** — current scope, can be built now
- **V4** — next version scope, note for design doc
- **V5** — distant scope, note but don't design for yet
- **Future** — beyond V5, worth remembering for long-term evolution
- **Never** — doesn't fit Genesis philosophy, explicitly reject with reason

### Phase Mapping
Map actionable items to specific Genesis build phases. If a finding is
"Phase 6 work," say so. If it requires a new phase or cross-cutting work,
flag it.

## Phase 4: Discussion & Refinement

When evaluating with the user:
- Present findings with the four-lens structure
- Invite pushback — user corrections improve the analysis
- Don't defend initial assessments defensively; update when wrong
- Surface non-obvious connections between findings

When evaluating autonomously (strategic reflection, surplus research):
- Apply the four lenses without interactive refinement
- Flag low-confidence assessments for user review
- Prioritize findings that affect current or next phase

## Phase 5: Documentation

### Living Design Document
Update `docs/plans/2026-03-08-research-insights-and-followups.md` (or create
a new dated document for major research sessions) with:

- Source and summary for each finding
- Four-lens evaluation results
- Architecture comparison tables
- Categorized follow-up items:
  - **Infrastructure** (near-term setup/integration)
  - **Architecture** (design changes, phase inputs)
  - **Research** (further investigation needed)
  - **UX** (dashboard, interface, user experience)

### Cross-Reference Updates
When findings affect existing design documents, update them:
- Build phases doc — new items for specific phases
- Agentic runtime doc — open questions, session config
- Gap assessment — newly identified gaps
- Memory files — key learnings for session persistence

### Action Items
Every follow-up item must have:
- Clear description of what to do
- Phase/version scope tag
- Dependency on other items (if any)
- Priority indicator (blocking vs nice-to-have)

## Anti-Patterns

### Do NOT:
- Dismiss things because they don't directly apply — check Lens 3 first
- Praise things because they're new or popular — check Lens 2 honestly
- Skip the competitive comparison out of politeness
- Assume our approach is better without evidence
- Assume their approach is better because they're bigger/funded
- Create scope creep by tagging everything as V3
- Write summaries instead of evaluations
- Forget to map findings to specific phases and design docs
- Give up on source access without exhausting alternatives
- Ask the user for help before trying all autonomous options

### Watch For:
- "This validates our architecture" — only if it actually does, with specifics
- "This is irrelevant" — did you check Lens 3 (how it COULD help)?
- "We should adopt this" — did you check Lens 2 (how it doesn't help)?
- Dismissing because of platform (headless server) without considering web UI
- Overcorrecting after user pushback (update the specific point, don't flip
  the entire assessment)
