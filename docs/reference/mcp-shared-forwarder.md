# Explicit shared-MCP forwarder

`scripts/genesis_mcp_forwarder.py` provides an explicit stdio connection to an
existing shared service. It uses the same four service names and trusted
`config/mcp_shared.json` plus install overlay as shared startup. It starts no
Genesis service, runtime, provider or Qdrant client. Ordinary registrations,
`adapter.argv`, the rollout ceiling and installed units stay unchanged.

This is an explicit shared-only connection. The complete local/shared tool
union, bookmarks, payment ancestry and per-tool stage routing
remain required before it can replace a registration. The shared catalog
deliberately excludes local tools. Native-client/provider qualification and
matched memory measurements remain activation gates.

The ordinary health executable also has the inactive health-read consumer
described below. This is one increment toward the complete four-service union;
the explicit CLI above remains shared-only.

The internal memory `RemotePeer` accepts an optional `pending_file` for the
caller bookmark contract. Default peers, including the explicit CLI and health
frontend, add no bookmark metadata or feature requirement. An opting-in memory
peer validates its path and requires exact integer
`plan_bookmark_pending_version: 1` from authenticated `/status` on every
connection generation, alongside existing service/transport/catalog checks.
Missing, mismatched or wrongly typed capability refuses before tool dispatch
when a logical generation is created. Opted-in peers also use memory's
`/mcp/v2` endpoint for requests, notifications and session cleanup, even when
no pending file currently exists. The same backend transport and resource owner
serve it. A pre-contract server without this route refuses the actual request
after an idle backend replacement; the client never falls back to `/mcp/v1`.
The payload version remains1. Default peers retain `/mcp/v1`. This route fences
the bookmark execution contract; dynamic catalog equality remains only a
logical-generation preflight. Only `tools/call` gains the reserved
`genesis/planBookmarkPending` metadata, with arguments, IDs and other metadata
preserved. A client-supplied reserved key refuses before network activity.
The ordinary memory frontend is the next consumer; this increment does not
select it or expand credentials. See [shared HTTP](shared-mcp-http.md).

The ordinary launcher exposes internal `_prepare_health` and `_prepare_memory`
helpers for managed consumers. Its existing `_bootstrap_health` and
`_bootstrap_memory` wrappers immediately run the returned module server with
their original transport options. Preparation is once per process: existing
profile middleware and degraded health setup remain eager; DB-backed resources,
read pools, bookmark policy and cleanup stay in the ordinary lifespan. Missing
memory DB still refuses, while health keeps its degraded mode. This preparation
seam does not itself implement the local/shared union. In particular, ordinary
health cleanup still owns process-global browser cleanup; any later composition
must settle all local browser work before that lifespan exits.

## Ordinary health read frontend

The exact ordinary `python scripts/genesis_mcp_server.py --server health`
command can host the local health server and forward its five health-floor
calls. Selection reuses P7's current config, environment, code ceiling and unit
readiness. The selected adapter command must equal that same ordinary command;
other adapter programs retain their own behavior. Endpoint and limit parsing
use that captured configuration. There is no inherited stage marker or new
`Decision` field. The second readiness query is read-only.

The shipped ceiling is still `off`, and `adapter.argv` remains empty. This
increment changes neither registrations nor installed units and does not raise
the ceiling. It admits health at `health-reads` only; memory, final `writes`,
bookmarks and native label/credential integration remain separate work. All
labelled/external-profile clients, extra argv and other transports keep their
ordinary path. Preflight refusal before preparation/input also keeps that path;
failure after the mixed owner starts ends that owner without replaying a call.

One prepared ordinary health server retains the complete advertised registry.
Every native request passes through its SDK connection before middleware
selects a remote tool. The SDK owns protocol-era rules, schemas, ordering,
pagination and malformed-request behavior. The frozen Catalog ancestry policy
keeps browser/module/payment tools local. Unknown names, including hidden hash
aliases outside that advertised catalog, return the ordinary unknown-tool
error without executing a body. This is not a general dynamic MCP proxy.

The local memory transport carries the original frame in a per-request
ContextVar, preserving integer/string IDs and user metadata without reading a
private SDK ID in production. Remote results use the public MCP-result wrapper;
local calls retain ordinary Context, progress, cwd and instrumentation. Remote
calls are instrumented by the service. There is one ordinary lifespan, whose
browser cleanup runs after local SDK work settles; it is never upgraded by
starting a second local server midway through a call.

Legacy initialization is forwarded before the FastMCP hook delegates, so a
remote refusal can prevent local state from committing. The SDK's typed
initialize request selects the legacy HTTP header, even if request metadata
carries a modern stamp. The bridge changes the transport header; parameter
values follow the existing SDK framing/trace normalization described below.
That header choice is retained for successful initialization replay. A modern connection's
rejected initialize leaves its later valid modern calls usable, as on ordinary
stdio. The existing shared-only CLI keeps its separate initialization policy
described later in this reference.

Each new mixed remote generation checks the existing authenticated `/status`
service, schema version, enabled state and caller-scoped catalog revision
against the local shared catalog. A mismatch refuses the generation without
executing locally. This is a preflight, not an atomic upgrade lock; an unrelated
non-routed schema change can also make the whole-catalog comparison refuse.

The intermediate health process still initializes ordinary health resources.
It establishes no memory saving or leak repair. Generic concurrent request
callbacks from two independently allocating peers remain unqualified; current
local Context uses are progress notifications. Payment peer constraints remain
unchanged, and disposable protocol fixtures do not establish native payment or
client acceptance. Final local ownership, remaining stages and matched
all-process measurements still gate activation.

## Explicit invocation

In the qualified environment, with a configured/running service and its existing
forwarder credential:

```bash
python scripts/genesis_mcp_forwarder.py --server health
```

`--server` accepts `health`, `memory`, `outreach` or `recon`. Optional `--url` must
equal that service's validated loopback URL. The script resolves source/config
from its own checkout, preserves the caller's environment/cwd and creates no
credential. All existing explicit service budgets are still required. This CLI
does not accept rollout stages or external-profile flags.

The same exact shared-stack fence applies: FastMCP 4.1.4, MCP 2.3.0 and Uvicorn
0.54.0. Ordinary floating dependencies remain supported by the ordinary stdio
path; an unqualified explicit forwarder refuses before opening its transport.

## Caller files and backend compatibility

The trusted service URL still ends in `/mcp`. The forwarder derives `/mcp/v1`
for default HTTP generations and cleanup requests. Opted-in pending-bookmark
memory peers use `/mcp/v2` instead. These routes use the same server, SDK session
manager and limits. An older backend without the versioned endpoint
refuses the connection before a tool runs; the frontend never downgrades or
resends a tool call after rollback.

Only immediate input-file fields are interpreted in the forwarder's cwd:

| Tool | Field | Caller interpretation |
|---|---|---|
| `evo_run`, `experiment_run` | `golden_set_path` | Nonempty paths become absolute; empty keeps the managed default |
| `skill_replay_run` | `suite_path` | Nonempty paths become absolute; empty keeps the managed default |
| `task_submit` | `plan_path` | Strip, expand `~`, make absolute; existing plan-root, content and intake checks remain |
| `document_index` | `path` | Expand `~` and make absolute, including empty cwd input |
| `document_query` | `path_or_doc_id` | Keep `pi-` IDs; otherwise expand `~` and make absolute |
| `knowledge_ingest_source` | `source` | Keep URLs accepted by the existing processor registry; otherwise make the read path absolute |
| `knowledge_ingest_batch` | `directory` | Make absolute, including empty cwd input |
| `resume_review` | `resume_source` | Existing caller paths become absolute; other strings remain resume text |

The golden/suite, ingestion and resume rules do not expand `~`. Absolute paths
retain symlink spelling; the tool's existing resolver and guards remain in
charge. Non-string arguments reach authoritative schema validation unchanged.
Remote-repository paths, glob patterns, durable strategy/charter references and
browser upload are outside this immediate shared-file mapping.

Rewritten ingestion arguments carry request-local original identity metadata:
cache keys, unit IDs, manifest sources and tree keys retain the supplied source
or directory spelling, while extraction and uploads read the caller's absolute
file. This adds no persistent alias mapping or cache migration. Optional
`read_path`/`read_directory` orchestrator arguments default to the existing
ordinary behavior; ordinary callers omit them.

The reserved `_meta["genesis/callerPath"]` hint is emitted only for rewritten
source/directory arguments. The backend checks its exact shape and binding to
the current argument. A missing optional hint on an absolute call is valid;
missing required request context or a malformed/misbound supplied hint refuses
before the file/provider consumer. Scope resets on success, error and cancellation.

On the retained `/mcp` route, unambiguous absolute paths, URLs, IDs and managed
defaults remain valid. Relative file arguments and relative resume strings
refuse rather than guessing a server-cwd interpretation. On `/mcp/v1`, a
relative resume string is text, so a same-named server file cannot replace it.
Ordinary stdio keeps its existing file/text interpretation.

## Identity and credentials

`caller_credentials.forwarder_headers()` reads only the existing `forwarder`
file in the P3 credential directory (`GENESIS_HOME` applies). It reuses the
existing owner/type/mode/token checks, repairs nothing and provisions nothing.
It validates and sends exactly P3's allowed identity fields from the central
caller accessor. Missing optional session/transcript IDs are not invented.
The server authenticates each request and remains the authority on profiles.

All non-null `CallerContext.profile` values are excluded from this forwarder. The
Codex label/credential-role integration and external-client routing remain
separately qualified work; retain the launcher's existing exclusions. Fixture
identity tests do not establish what every native client inherits or how `/clear`
refreshes its environment. Credential/config changes retain their operator
restart policy; this adds no hot-reload or credential-rotation command.

## Requests, failures and retirement

The qualified SDK owns JSON framing, request correlation and HTTP transport. Raw
admission and strict UTF-8 run before its decoder, preserving the shared edge's
duplicate-field, unsafe-ID and task-augmentation refusals. Integer/string IDs
remain unchanged in both directions. Valid payload values and nested metadata
are forwarded; optional empty/null fields and trace propagation follow SDK
normalization rather than a byte-identical framing promise.

Legacy initialization uses one completion/outcome gate while the receive loop
continues observing EOF. A failed handshake does not release queued tools.
Modern requests can run without initialize. A later connection repeats only
successful initialization control state. A different negotiated protocol yields
an explicit restart-client error before a tool is sent under it.
The modern protocol has no initialize handshake: an explicit modern initialize
keeps the service's refusal and does not release queued tools. Actual P4
corruption tests cover modern call/list and legacy call/list/initialize;
synthetic HTTP tests also exercise initialization response-error handling.

No `tools/call` is automatically resent, including nominal reads that update
counters. An uncertain transport/parse outcome returns the original request ID
with `Outcome unknown; not resent`. A safe code accompanies that error; parser
input excerpts do not. A native caller deciding whether to repeat a write must
inspect its outcome separately. Ordinary backend refusals remain refusals:
in particular, the service's `-32000` busy response does not close a connection
or cancel neighboring calls merely because SDK closure uses the same code.
Qualified JSON/SSE parser failures on other methods also retain the error code
and use a safe response-error message without the parser's input excerpt.
Request HTTP 202, unexpected content types and unfollowed redirects also produce
safe transport failures, with the same unknown wording for tool calls. Redirects
are disabled through the owned client's public `max_redirects=0` setting: the
qualified SDK otherwise follows some redirects by resending the original POST,
independently of the client's `follow_redirects` setting. Real backend JSON-RPC
errors keep their code, message and data; classification uses qualified SDK
code/message pairs, rather than treating every refusal code as a lost outcome.

An idle legacy session can first return `Session not found`; that request is
not replayed. A subsequent request can establish a fresh session. There is no
adapter retry timer or background reconnect loop.

Tool reads have no added HTTP deadline. Connection/write/pool operations use the
SDK's named default. The public SSE event-size limit is disabled to preserve
existing stdio response capability; the shared service's configured request-body,
session and concurrency limits still apply.

Stdin EOF cancels local handlers and retires the connection. The HTTP writer
stays alive during the bounded control-only DELETE attempt, allowing queued
legacy courtesy cancellation to arrive. That attempt reads the existing service
`shutdown_grace_s`; zero gives no delivery confirmation. Failed cleanup does not
claim the remote session was deleted. Remote idle expiry remains server-owned.

For native POSIX pipes, nonblocking output and explicit stdio cancellation let
EOF retire a frontend whose stdout reader stays open but stops consuming. A
partially written final frame may be abandoned after input ends. This is not a
guarantee of a complete reply to a departed caller or cancellability of a stalled
host kernel/storage operation. SDK fd0/fd1 claim/diversion helpers are pinned
compatibility seams; their original blocking state and descriptors are restored.
Raw refusal writes belong to the existing task group so they cannot stop the
input reader from seeing EOF behind a full output pipe. This bounds their
lifetime; it adds no numeric client task-admission limit.

The owned CLI suppresses SDK protocol/body diagnostics and emits safe adapter
failure categories. Neither credentials, forwarded identities nor parser body
excerpts are intended for stderr. Protocol errors still reach the native wire.
