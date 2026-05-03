# ADR 005: No Timeouts Without Explicit User Approval

**Status:** Accepted
**Date:** 2026-04-10

## Context

In a 24/7 autonomous system, timeouts seem like defensive best practice —
prevent hangs, bound resource usage, ensure responsiveness. But Genesis's
cognitive paths (reflection, deep thinking, research) have unpredictable
durations that are often legitimate.

## Decision

No timeout may be added to any Genesis code path without explicit user
approval. This includes `asyncio.wait_for`, `asyncio.timeout`, stream idle
timeouts, subprocess timeouts, and watchdog thresholds.

## Consequences

**Benefits:**
- Legitimate long thinking is never capped by an arbitrary ceiling
- Forces explicit discussion of what "too long" means for each case
- Prevents speculative defense-in-depth from introducing production failures
- Each timeout has documented justification (the failure mode it prevents,
  evidence the failure is real)

**Costs:**
- Genuine hangs take longer to detect (mitigated by health monitoring)
- Developer friction — can't add "just in case" timeouts
- Requires alternative approaches for resource bounding (budget limits,
  health checks, manual intervention)

**Why this is necessary:** Early in development, speculative timeouts on
reflection and CC call paths caused more production issues than the hangs
they were meant to prevent. A 30-second timeout on a deep reflection that
legitimately takes 45 seconds silently kills valuable work. The system was
fighting itself. Now, if a timeout is genuinely needed, it must be proposed
with: the specific value, the failure mode it addresses, and evidence that
the failure is real (not hypothetical).
