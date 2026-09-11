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
  worse than without it. Size: `GENESIS_RECALL_READ_POOL_SIZE`; the default is
  DERIVED from the host's CPU count, clamped to 4..12 (`derive_read_pool_size`),
  because the pool size is a hard concurrency ceiling — the (size+1)th
  simultaneous recall **blocks on checkout**, so a pool smaller than the number
  of sessions each firing a per-prompt recall converts into request-budget
  timeouts. MEASURED: with a fixed pool of 4, 503s appeared exactly past 4
  concurrent recalls (0 at 1/2/4, 1/16 at 6, 7/16 at 8). Kill switch
  `GENESIS_RECALL_READ_POOL_OFF=1`. The query-embed call-site heartbeat
  is also fired off the hot path, so `embed` no longer blocks on that write.

## Output budget — every model-facing write is bounded

This is a `UserPromptSubmit` hook, so everything it prints is bare stdout that
the model reads. The harness FILES a hook entry over `HOOK_STDOUT_CAP` behind a
~2 KB preview, with no error and no exit-code change — so an overrun silently
costs the window its peer list and its prompt-injection safety directive, and
this hook's own code notes that a failed peer read "reads exactly like no
concurrent sessions".

Every write therefore goes through one `BoundedStdout`
(`scripts/hooks/hook_output.py`), and each contributing surface is bounded **by
meaning** rather than by one blanket character cap:

| surface | bound | why that shape |
|---|---|---|
| extracted keywords | length window, `_MAX_KEYWORD_CHARS` | the ROOT. An identifier cannot get long here (every non-alphanumeric char becomes whitespace, so identifiers SPLIT), but an unbroken alphanumeric run — a pasted hash, token, or minified blob — is one token of whatever length. DROPPED, not truncated: a keyword is a KEY (`_detect_pivot` compares keyword sets), so a truncated id would collide with a different id sharing its prefix. |
| `[Session trail]` | `_MAX_TRAIL_LINE_CHARS` | drops WHOLE oldest pivots and marks it with the `… →` prefix the count bound already uses. A truncated arrow chain would end in half a topic that reads like a whole one. |
| `[Code]` hints | `_MAX_CODE_HINT_CHARS` | the largest surface on REAL data. Clips the signature and keeps the file location whole — the location is the actionable half. |
| `[Concurrent]` peers | `_MAX_PEERS_SHOWN` (a query `LIMIT`) | bounded by COUNT so the safety directive that follows always has room. Overflow is NAMED from a `COUNT`, never inferred from a read that stopped at its own limit. |

`tests/test_scripts/test_hook_output_contract.py` fails this hook if any
model-facing `print` reappears; `tests/test_hooks/test_proactive_hook_bounded_output.py`
pins the behaviour of each bound above.

**Units.** The two SIZE bounds (trail line, code hint) are measured in UTF-16
code units — what the harness bills — via `utf16_len` and `clip_to_cost`. The
keyword window is deliberately in CODEPOINTS, because it asks whether a token is
a word or a pasted blob, and billing a 20-character astral CJK word as 40 units
would drop a real word for being non-Latin; `_detect_pivot`'s filter must use the
same unit or the two disagree about what is in the window. Mixing the units does
not loosen a bound, it skips it: the code hint originally billed in UTF-16 while
deciding in `len`, so 200 emoji rendered 626 units against a stated 400 and were
never clipped. The breach lives in the MIDDLE of the range — a huge value trips
the guard and clips correctly — so any probe here sweeps widths.

**Testing notes.**

- The writer is a module-level singleton — correct in production (one hook run
  per process), wrong under pytest, where every test would share one budget.
  `tests/test_hooks/conftest.py` resets it per test, and warns loudly if it
  cannot, because a silent failure there surfaces as an unrelated test failing on
  a cut it did not cause.
- That reset only works while there is ONE module object. A test file that
  rebuilds the hook with `importlib` and assigns over `sys.modules` creates a
  second copy, and the two halves of the suite then run different code —
  `test_intent_trail.py` did this, and it cost eight unrelated tests their output
  the moment the hook gained a writer.
- Labels are PERSISTED and survive 60 days, so a trail written before these
  bounds existed holds unbounded labels. Anything reading `intent_trail.json`
  must assume it was written by older code — `_detect_pivot` and
  `_render_trail_line` both do.

## Related

- Endpoint + engine: `src/genesis/dashboard/routes/proactive.py`,
  `src/genesis/memory/proactive.py`, `src/genesis/mcp/memory/core.py::_proactive_impl`.
- The memory-system layer model (L1–L4) is in the project `CLAUDE.md`.
