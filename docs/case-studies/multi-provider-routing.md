# Provider Outages, Zero Interruption

Any system that depends on one LLM provider is one outage away from total failure.
That's not theoretical. Anthropic goes down. OpenAI goes down. Every major provider
has had outages, some lasting hours. Route everything through one of them, and that
outage becomes your outage.

Genesis doesn't share that fate.

---

## The Scenario

It's Tuesday afternoon. Genesis is running: a triage cycle in progress, a reflection
queued, a recon job executing. Anthropic's API starts returning 503s. The primary
call site fails once, twice, a third time. The circuit breaker trips. Genesis doesn't
pause to wait for Anthropic to recover.

The fallback chain activates. For that call site, the next provider might be Groq,
Mistral, or Cerebras, ordered by cost and capability and configured ahead of time.
The request routes to the next provider. The triage cycle completes. The reflection
runs. The recon job finishes. The user sees nothing.

That's what a 20+ provider fallback architecture looks like in practice.

---

## How It Works

Three mechanisms run on every LLM call.

**Circuit breakers** track consecutive failures per provider. Three failures trip the
circuit, and that provider gets skipped entirely until it stabilizes: 120 seconds on
the first trip, doubling with each subsequent failure, capped at 30 minutes. Failed
retries against a down provider waste time and burn rate budget. The breaker
short-circuits that pattern and falls through to the next option immediately.

**Rate gates** prevent thundering herd. When a primary provider fails, every pending
call simultaneously tries the same fallbacks. Without coordination, those fallbacks
hit their rate limits and fail too. Genesis serializes requests per provider using a
per-provider asyncio lock. The cascade stops.

**Fallback chains** are call-site-specific. Triage calls and embedding calls have
different provider requirements. Each call site has its own ordered list, so a failure
in one call site degrades that path to its next option without affecting others.

When every provider in a chain fails, which happens during large coordinated outages,
requests go into a dead-letter queue rather than being dropped. They replay when
providers recover.

---

## The Outcome

From the user's side: Genesis keeps working. Not "mostly keeps working." Keeps
working, because any single provider outage still leaves 19+ others available.

From the system side: circuit state persists across restarts, so a reboot doesn't
reset an open breaker and retry a dead provider. Budget stays intact, because free-tier
providers absorb volume when paid providers are down. And the degradation logic matches
response to severity: one provider down means normal operation; two paid providers
down pauses non-critical jobs to preserve rate budget for triage and embeddings;
all paid providers down triggers essential-only mode until they recover.

---

## Why This Matters

Single-provider dependency is the norm because it's simpler. One API key, one
integration, one failure point. The downside only surfaces during outages, and
outages feel rare until you're running something continuously.

Genesis makes ~100 LLM calls per day. Over any multi-month stretch, provider outages
are a statistical certainty. An always-on system that can't survive provider failures
isn't really always-on. The routing architecture is one of the most invisible parts
of Genesis, which is exactly the point: when it works, you don't notice it.

---

*For the implementation details behind this case study, see
[`docs/architecture/routing-deep-dive.md`](../architecture/routing-deep-dive.md).*
