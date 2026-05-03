# ADR 002: Surplus Uses Router Directly, Not CC Sessions

**Status:** Accepted
**Date:** 2026-04-20

## Context

Surplus compute (brainstorms, enrichment, code audits) needs LLM capabilities
during idle periods. Two options: spawn full Claude Code sessions (with tool
access, file I/O, conversation loop) or call the LLM router directly with
structured prompts.

## Decision

Surplus tasks call `router.route()` directly with tier="free" (or paid for
specific call sites). They do NOT spawn CC sessions.

## Consequences

**Benefits:**
- Fast — no CC session startup overhead (seconds vs minutes)
- Cheap — free-tier models handle most surplus work
- Contained — no file system side effects, no tool access needed
- Parallelizable — multiple surplus tasks can run concurrently without session conflicts
- Budget-controlled — router respects cost tracking and circuit breakers

**Costs:**
- No tool access — surplus can't browse, edit files, or run commands
- Single-turn only — no multi-step reasoning chains
- Model quality ceiling — free-tier models produce lower quality for complex tasks

**Exception:** Tasks requiring tool access (code audits needing file reads,
research needing web search) escalate to `direct_session` dispatch through
the ego subsystem. This is a deliberate escalation, not the default path.

**Why this matters:** Reflection routing was initially confused with surplus
routing. Reflections (which process awareness signals) are a cognitive path
that routes by output type. Surplus is opportunistic compute that routes by
cost tier. Conflating them caused reflection outputs to be treated as surplus
tasks and vice versa.
