# Peer execution containment

This is opt-in internal groundwork. No peer endpoint or background-runner branch
is enabled by this slice. A later task coordinator supplies authorized context
and the lease broker; process cleanup alone is insufficient to release capacity
while broker operations remain in flight.

`CCInvocation.peer_segment` requires an immutable `PeerSegment`, explicit system
prompt and absolute working/config paths, external-untrusted origin and a timeout
of at most 7,200 seconds. It rejects permission skipping, owner resume, appended
context, skills, tool/environment overrides and incompatible CLI modes. Launch
revalidates the frozen invocation's containment policy before building argv.

The owner-created MCP JSON must be a regular file owned by the process user,
mode 0600, no symlink, at most 4 KiB, containing only:

```json
{"mcpServers":{"genesis_peer":{"command":"<sys.executable>","args":["-m","genesis.peers.facade","--lease-file","<absolute lease path>"]}}}
```

The facade entry point and lease file are supplied by the later broker slice.
No token value belongs in argv or this configuration. The lease path is internal
and is not exposed in peer results. The configuration supplies no environment
overrides or additional servers.

Peer prompt/output/error prose is excluded from invoker logs and trace status
messages. Error redaction propagates to ancestor spans while exception types
and internal classification remain available to the coordinator.

The CLI uses strict MCP configuration, empty builtin tools and setting sources,
`dontAsk` permission handling and exact facade tool names. Slash commands, Chrome
and session persistence are disabled. Explicit identity/context replaces the
default system prompt; this module does not assemble owner memory or history.
The environment preserves HOME and native provider authentication, uses existing
roster credential routing, and excludes owner Genesis/GitHub credentials and
shell startup/session settings. Existing child environment pins still apply.

Each segment uses `genesis-peer-<32 lowercase hex>.scope` under the user systemd
manager, bound to `genesis-server.service` with `BindsTo` and `After`. RuntimeMaxSec
is the remaining deadline, capped at 7,200 seconds; stop timeout is ten seconds,
with control-group killing and SIGKILL enabled. There is no unscoped fallback.

Cleanup runs on success, errors and cancellation in both public invoker paths,
shielded against repeated caller cancellation. A still-tracked control group must
be removed or have `cgroup.events` report `populated 0` after stop. A confirmed
collected scope, or inactive/failed scope whose path systemd has cleared after
recursive emptiness checks, is also complete. Failed state alone is insufficient.
Unavailable manager state, active scopes with missing paths, unavailable cgroup v2
and populated descendants raise; the coordinator must retain/reconcile capacity.

Runtime probes use scratch scopes and local provider fixtures. They do not stop
the production server or prove live peer enrollment, authority grants or broker
drain; those require their own dependent acceptance tests.
