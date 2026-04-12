---
name: obstacle-resolution
description: Resolve obstacles using fallback chains — use when an approach fails, a dependency is unavailable, an API returns errors, or a task is blocked and needs an alternative path forward
consumer: cc_background_task
phase: 6
skill_type: workflow
---

# Obstacle Resolution

## Purpose

When Genesis encounters a blocker — a failed API call, an unavailable service,
a missing capability — systematically resolve it using the fallback chain
framework.

## When to Use

- A routing chain is exhausted (all providers failed).
- A required service is unreachable.
- A task cannot proceed due to a missing dependency or capability.
- Automatic retries have been exhausted.

## Workflow

1. **Classify the obstacle** — What type? (provider failure, data missing,
   capability gap, external dependency, permission issue)
2. **Check fallback chain** — Load the relevant fallback chain from
   `fallback_chains.py`. Walk the chain in order.
3. **Attempt each fallback** — Try each alternative. Log attempts and results.
4. **Escalate if needed** — If all fallbacks exhausted:
   - For non-urgent: queue for user review, continue with degraded capability.
   - For urgent: alert user immediately via outreach.
5. **Record resolution** — Store the successful resolution path as an
   observation. If a new fallback was discovered, propose a procedure update.

## Output Format

```yaml
obstacle: <one-line description>
date: <YYYY-MM-DD>
type: provider_failure | data_missing | capability_gap | external_dep | permission
chain_attempted:
  - step: <fallback step>
    result: success | failure
    detail: <what happened>
resolution: resolved | degraded | escalated
resolution_detail: <how it was resolved>
```

## Examples

### Example: Embedding provider chain exhausted

**Trigger:** memory_store fails with EmbeddingUnavailableError — Ollama timeout,
DeepInfra 429, DashScope connection refused.

**Expected output:**

```yaml
obstacle: All embedding providers exhausted during memory store
date: 2026-03-20
type: provider_failure
chain_attempted:
  - step: Ollama (local)
    result: failure
    detail: ReadTimeout after 60s — model not loaded
  - step: DeepInfra (cloud)
    result: failure
    detail: HTTP 429 rate limit (RPM exceeded)
  - step: DashScope (cloud)
    result: failure
    detail: ConnectionRefusedError — service unreachable
resolution: degraded
resolution_detail: Memory stored FTS5-only (no vector). Queued in
  pending_embeddings for background recovery. Circuit breaker tripped
  on DashScope (120s backoff).
```

## References

- `src/genesis/learning/fallback_chains.py` — Fallback chain definitions
- `src/genesis/routing/` — Router and circuit breaker for provider failures
- `src/genesis/routing/degradation.py` — Degradation levels
