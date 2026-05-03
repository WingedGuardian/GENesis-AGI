# ADR 001: Ego Sessions Are Ephemeral

**Status:** Accepted
**Date:** 2026-04-28

## Context

Genesis's ego subsystem drives autonomous decision-making — proposing actions,
evaluating priorities, and dispatching work. The question: should ego maintain
persistent conversation context across ticks (like a long-running CC session),
or start fresh each cycle?

## Decision

Ego sessions are ephemeral. Each tick reconstructs context from:
- Essential knowledge (system state summary, ~300 tokens)
- Proactive memory recall (recent relevant memories)
- Bulletin board (pending proposals, outcomes of previous decisions)
- Current system health and pending approvals

No conversation history carries between ticks.

## Consequences

**Benefits:**
- No context rot — decisions are always based on current state, not stale assumptions
- No compounding hallucination — each tick's reasoning is independent
- Cheaper — no long context windows accumulating over hours/days
- Crash-resilient — server restart loses nothing, next tick continues normally
- Testable — each tick is a pure function of its inputs

**Costs:**
- Can't maintain multi-tick reasoning chains (must use bulletin board for continuity)
- Loses nuance from previous deliberations (compensated by memory store)
- Each tick pays the "cold start" cost of context reconstruction

**Why not persistent:** A 24/7 system that thinks every few minutes would
accumulate thousands of messages. Context windows have limits, and even with
compression, old context biases new decisions. Ephemeral-with-memory-bridge
gives the benefits of persistence without the rot.
