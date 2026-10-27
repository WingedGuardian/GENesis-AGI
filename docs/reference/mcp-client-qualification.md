# Native MCP client qualification

`tests/test_hooks/test_codex_mcp_transport_native.py` runs an explicitly supplied
native Codex binary against a synthetic localhost MCP service with an isolated
HOME, CODEX_HOME and workspace. Its local model-provider sink must receive zero
requests. Use the actual installed native binary, not a wrapper with unrelated
setup side effects. The first probe covers same-thread restart and catalog
change with explicit MCP configuration reload, in stateful and stateless modes.

```bash
GENESIS_CODEX_NATIVE_TESTS=1 \
GENESIS_CODEX_APP_SERVER_BIN=/absolute/path/to/native/codex \
  .venv/bin/python -m pytest tests/test_hooks/test_codex_mcp_transport_native.py -q
```

The fixture owns its localhost listener and child processes. Evidence is saved
in the pytest temporary directory and printed with `-s`; retain stdout in a
new private file outside the repository. The repository's
`tmp_path_retention_policy = "failed"` removes passing test directories, and
its per-process basetemp cleanup removes that temporary tree at exit. Do not
rely on temporary files as durable evidence.
These probes are not qualification of real shared Genesis services. Caller
identity, concurrent database writes, interruption ambiguity and client/resource
lifecycle need their own evidence before activation. A failed probe is a
finding to investigate, not permission to reconnect live clients or replay a
possibly committed tool call.

The additional opt-in files `test_codex_mcp_multiclient_native.py` and
`test_codex_mcp_interruptions_native.py` cover two native connections, parallel
caller headers, bad credentials, ten create/unsubscribe cycles, interrupted
scratch writes and stdio EOF. A
five-second observation after unsubscribe is explicitly shorter than native
idle retention; retained threads at that point are not classified as leaks.

For read-only process observations, see [the process report](mcp-process-report.md).

## Qualification finding: existing-thread catalog

On Codex 0.164.0 with FastMCP 2.14.6 / MCP 1.26.0, the synthetic restart probe
reproduced a stale existing-thread catalog in both stateful and stateless HTTP:

- Calls to the restarted service succeeded, including a newly added tool.
- `mcpServerStatus/list` with both `threadId` and `serverName` still omitted
  that tool, before and after successful `config/mcpServer/reload`, and after
  a five-second observation window.
- An unscoped inventory query and a fresh thread reported the new tool.

The installed schema says that the server-specific query reuses the thread's
connection. Therefore a successful independent inventory query or direct call
does not establish that the existing thread's catalog refreshed. The reload
API queues a refresh. A subsequent, separately approved probe ran one mocked
turn with both HTTP modes configured in the same existing thread. After reload,
the tool runtime inside that turn still advertised only the original tools;
attempting each new tool returned `TypeError: ... is not a function`. Both
thread-scoped catalogs remained stale after the turn. The turn itself completed:
the fixed mock captured those invocation errors and returned a final message.

That probe used exactly one `turn/start` and two localhost mock responses, with
no real model/API calls. Its reviewed source was unchanged during execution.
It is separately enabled by `GENESIS_CODEX_MOCK_TURN_TESTS=1`; the ordinary
native-test opt-in does not enable it.

A second one-turn probe emitted `notifications/tools/list_changed` over SSE
responses after adding a tool at runtime. The fixture observed notifications
written in both modes, but no additional `tools/list` requests through a
five-second grace and the next turn. Existing-thread catalogs and inside-turn
tool availability remained unchanged. This separately opt-in diagnostic uses
`GENESIS_CODEX_NOTIFICATION_TURN_TESTS=1`; neither mocked-turn diagnostic is
enabled by the ordinary native-test opt-in. Both diagnostics assert refresh
and fail on the measured version; they are not the accepted qualification bar.

The accepted recovery contract now permits existing threads to retain their
starting catalog, matching the current per-session stdio expectation. Fresh
threads must see changes; calls to removed or renamed tools in old threads must
return clear errors without hanging or crashing. The final synthetic matrix
passed this contract on the measured client. This does not prove that an adapter refreshes catalogs or
qualify real Genesis memory concurrency. P2b per-request database isolation is
required before any shared-memory activation.
See the [app-server API](https://learn.chatgpt.com/docs/app-server).

## Measured acceptance, 2026-10-27

After internal architecture and security review, the exact-source matrix passed
23 tests in 111.77 seconds with no warnings or model requests. Both separately
opted-in mocked-turn diagnostics were skipped; their earlier negative results
remain the evidence above. Source hashes were checked unchanged after execution.

- Both existing clients recovered across same-catalog and changed-catalog
  restarts in stateful and stateless HTTP, preserving their distinct caller headers.
- Fresh threads saw additions/removals/renames. Old-thread removed/renamed calls
  returned explicit unknown-tool errors; subsequent healthy calls succeeded.
- Before/after durable-write interruptions produced errors and zero/one writes,
  respectively, with no implicit replay in either HTTP mode.
- Stdio EOF exited with status zero; the captured responding fixture and other
  observed children had no same-identity survivors after five seconds.
- Ten unsubscribes left 13 loaded threads versus three initially in each mode
  after five seconds. This short window does not establish a leak.

All nine native matrix cases also rejected deliberately falsified observations
in a bounded mutation run. These results qualify the synthetic transport contract,
not shared Genesis database behavior. Independent review and normal PR gates
remain separate requirements.
