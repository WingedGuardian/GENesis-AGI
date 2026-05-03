# ADR 006: Router Dead-Letter Queue for Failed LLM Calls

**Status:** Accepted
**Date:** 2026-03-20

## Context

Genesis makes 100+ programmatic LLM calls daily across 33 active call sites.
Providers experience transient failures (rate limits, network issues, model
overload). In a 24/7 system, failed calls represent lost work if silently
discarded.

## Decision

The router maintains a dead-letter queue (DLQ). Failed calls that exhaust
the fallback chain are captured with full context (call site ID, messages,
parameters, failure reason, timestamp) for later analysis or replay.

## Consequences

**Benefits:**
- No silent work loss — failed cognitive tasks are recoverable
- Debugging visibility — DLQ reveals patterns (which providers fail, when,
  which call sites are affected)
- Circuit breaker integration — DLQ growth signals systemic provider issues
- Enables deferred retry — batch retry during provider recovery

**Costs:**
- Storage growth — DLQ accumulates if not processed/pruned
- Stale context — old DLQ entries may no longer be relevant
- Replay complexity — not all calls are safely replayable (side effects)

**Why not just retry immediately:** Immediate retry during provider outages
wastes budget and may hit rate limits harder. The DLQ pattern separates
failure detection from recovery, allowing smarter retry strategies (wait for
circuit breaker recovery, use different provider, batch replay during low-load).
