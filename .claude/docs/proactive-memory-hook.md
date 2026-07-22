# Proactive Memory Hook (thin client)

`scripts/proactive_memory_hook.py` is the Claude Code `UserPromptSubmit` hook
that injects relevant memories before each of your prompts. Since the
thin-client flip it is a **client of the genesis-server recall engine**, not a
reimplementation of it.

## How it works

1. **Session-local awareness** (always, in-process): heartbeat + concurrent-
   session tags, intent-trail/pivot detection, recent-activity summary, the H-1
   working-set measurement, and the ambient session-awareness fold.
   The hook also runs a local `code_symbols` lookup and prints any `[Code]
   symbol — location` structural hints on the server path — the server engine
   surfaces semantic memory only, so this cheap local lane (which the pre-flip
   fork fused) stays hook-side.
2. **Recall** (delegated): the hook POSTs `{prompt, session_id, profile:"cc_hook",
   file_keywords, suppress_ids}` to `POST /api/genesis/hook/recall`. The server
   engine (`genesis.memory.proactive.proactive_context`) runs the full pipeline —
   FTS5 + vector recall, reranker, entity lane, graph expansion, injection
   defense, intent-aware budget, procedure surfacing — and returns print-ready
   `lines` plus structured `results`, `procedure`, `shadow`, and the prompt
   `embedding` (which feeds the ambient fold). The hook prints the lines and
   records the working-set measurement.

Because recall lives in exactly one place, every memory improvement (reranker,
graph expansion, new lanes) reaches the hook automatically — no more shipping
each change twice.

The endpoint path also re-applies the two content-quality guards the old fork
had (they were hook-only, never in the shared `memory_proactive` MCP tool):
malformed rows (`provenance.is_garbage` — raw JSON observation blobs, YAML
frontmatter, NULL) and non-intentional `knowledge_base` hits (only
`extraction_job`/`knowledge_ingest`/`knowledge_ingest_source`/`reference_store`/
`curated` — the dashboard file/URL upload pipeline — survive; the collection is
otherwise majority surplus/recon crawl). These run
inside `_proactive_impl` (gated by a `filter_noise` flag the endpoint sets and
the MCP tool leaves off) — in the backfill loop and before external-content
wrapping, so a dropped noisy hit is replaced by the next safe candidate and the
garbage check sees raw content. Predicate: `provenance.is_proactive_noise`.

## Modes — `GENESIS_PROACTIVE_HOOK_MODE`

| value | behaviour |
|-------|-----------|
| `server` (default) | call the endpoint; degrade to FTS5 on any failure |
| `local` | skip the endpoint, always use the FTS5 degraded path |
| `off` | session-local awareness only, no memory recall |

`GENESIS_PROACTIVE_HOOK_URL` (default `http://127.0.0.1:5000`) points at the
local genesis-server; override it if you run the server on a non-default port.

## Degraded fallback

On any server failure (connection refused, timeout, non-200, bad JSON) the hook
falls back to a **keyword-only FTS5 search** of `episodic_memory` and prints a
visible banner + `[Memory·degraded | …]` tags. The banner **names the actual
cause** rather than always saying "unreachable": a genuinely down/restarting
server (connection refused / connect timeout) reads `genesis-server unreachable`,
while a *reachable* server that returned a 503 (recall over its 4.5s budget or
still booting), timed out, or errored reads `server returned HTTP 503
(reachable …)` / `recall timed out … (server reachable …)`. This stops a slow-or-
busy recall from being mislabeled as a dead server (and from masking the latency
signal). The fallback does **no**
write-backs, but it still re-applies the external-world provenance label
(`Memory·external`) and emits the gate-4 injection-shadow record for any
blockable content it injects locally — the same injection-defense invariant the
server path enforces. It self-heals on the next prompt once the server is back.

## Observability

`~/.genesis/proactive_metrics.json` records the latest invocation, including
`mode` (`server`/`degraded`/`local`/`off`) and `server_ms`, so the server-path
fallback rate is directly observable. The health dashboard reads this file.

The server response carries `timings_ms` with a per-stage breakdown —
`embed`, `recall`, and (since ac27b693) the recall sub-stages `vector`,
`event`, `expand`, `fts`, `expired`, `activation`, `breadcrumbs`, `assembly`,
plus `rerank`, `enrich`, `procedure`, `total`. The read-stage timers
(`event`/`fts`/`expired`/`activation`/`breadcrumbs`) decompose the `recall`
bucket so what used to be an unaccounted residual — read-lock contention on the
shared connection — is now attributable to a stage. When a call exceeds the
slow-log threshold the server writes one `proactive recall slow: {…}` INFO line,
so a latency regression is attributable to a stage from the journal alone
without live probing.

## Latency: work off the hot path

The per-prompt path is latency-budgeted (the route's 4.5s bound), so the server
does the least work needed to build the response and defers the rest:

- **Write-backs and eval emits are deferred.** The `retrieved_count` bumps, the
  J-9 `recall_fired` + diagnostics events, the entity-lane shadow probe, the
  injection-gate immunity emit, and the procedure `surfaced_count` bump all run
  on background tasks AFTER the response returns — they never affect what is
  injected. A fixed in-flight backstop makes recall fall back to running them
  inline (rather than piling up) if they ever drain slower than prompts arrive.
  Deep-search recall (`memory_recall` MCP) keeps them inline.
- **The tag co-occurrence index refreshes in the background** (stale-while-
  revalidate): a stale index never blocks a prompt on a full-corpus scroll; the
  current prompt uses whatever the index holds and a single background task
  rebuilds it.
- **Recall reads run on a dedicated read-only connection pool** (`mode=ro`,
  WAL-aware; `db/connection.py::ReadConnectionPool`, wired in `init/memory.py`).
  All Genesis subsystems share one write connection behind a single lock, so
  recall's read stages (FTS5, activation, enrich, breadcrumbs) otherwise queue
  behind the whole server's writes under concurrent sessions. The pool gives
  them genuinely-parallel readers off that lock. It is an **optional value-add**:
  any pool miss or error falls back to the shared connection, so recall is never
  worse than without it. Size: `GENESIS_RECALL_READ_POOL_SIZE` (default 4); kill
  switch `GENESIS_RECALL_READ_POOL_OFF=1`. The query-embed call-site heartbeat
  is also fired off the hot path, so `embed` no longer blocks on that write.

## Related

- Endpoint + engine: `src/genesis/dashboard/routes/proactive.py`,
  `src/genesis/memory/proactive.py`, `src/genesis/mcp/memory/core.py::_proactive_impl`.
- The memory-system layer model (L1–L4) is in the project `CLAUDE.md`.
