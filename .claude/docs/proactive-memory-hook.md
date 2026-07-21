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
`extraction_job`/`knowledge_ingest`/`knowledge_ingest_source`/`reference_store`
survive; the collection is otherwise majority surplus/recon crawl). These run
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
visible banner + `[Memory·degraded | …]` tags. The fallback does **no**
write-backs, but it still re-applies the external-world provenance label
(`Memory·external`) and emits the gate-4 injection-shadow record for any
blockable content it injects locally — the same injection-defense invariant the
server path enforces. It self-heals on the next prompt once the server is back.

## Observability

`~/.genesis/proactive_metrics.json` records the latest invocation, including
`mode` (`server`/`degraded`/`local`/`off`) and `server_ms`, so the server-path
fallback rate is directly observable. The health dashboard reads this file.

## Related

- Endpoint + engine: `src/genesis/dashboard/routes/proactive.py`,
  `src/genesis/memory/proactive.py`, `src/genesis/mcp/memory/core.py::_proactive_impl`.
- The memory-system layer model (L1–L4) is in the project `CLAUDE.md`.
