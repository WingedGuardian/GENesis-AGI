# ADR 007: LLM-First Judgment, Code-Only Structure

**Status:** Accepted
**Date:** 2026-03-10

## Context

Genesis needs to make judgment calls: Is this memory worth storing? Is this
observation high-priority? Should this surplus task run now? Should this
outreach message be sent? These decisions could be encoded as heuristics
(if/else rules, scoring thresholds, keyword matching) or delegated to LLMs.

## Decision

Code handles structure — timeouts, validation, event wiring, data flow,
scheduling. Judgment calls belong to the LLM. When a decision requires
understanding context, nuance, or intent, route it through the LLM router
rather than building heuristic code.

## Consequences

**Benefits:**
- Judgment improves automatically as models improve (no code changes needed)
- Handles edge cases that heuristics miss (context-dependent decisions)
- Reduces code complexity — no sprawling rule engines
- Better prompts > better heuristics (cheaper to iterate)
- Staircase principle: start simple (zero-shot prompt), only add complexity
  with evidence the simple approach fails

**Costs:**
- Latency — LLM calls are slower than local heuristics
- Cost — each judgment call costs tokens (mitigated by free-tier routing)
- Non-deterministic — same input may produce different judgments
- Debugging opacity — harder to trace why a specific decision was made

**Where code-based rules ARE appropriate:**
- Structural validation (JSON schema, required fields)
- Rate limiting and circuit breaking
- Budget enforcement (hard numerical thresholds)
- Scheduling and orchestration (cron-like timing)
- Health checks (binary up/down determination)

**The test:** "Would a thoughtful person need context and judgment to make
this decision?" If yes → LLM. If a simple rule suffices → code.
