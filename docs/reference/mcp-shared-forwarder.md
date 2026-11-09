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
for every HTTP generation and cleanup request. Both routes use the same server,
SDK session manager and limits. An older backend without the versioned endpoint
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
