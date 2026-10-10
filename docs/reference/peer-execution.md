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
Peer invocation configuration uses JSON output; the streaming method performs
its own stream-json override with the required verbose flag.

The owner-created MCP JSON must be a regular file owned by the process user,
mode 0600, no symlink, at most 4 KiB, containing only:

```json
{"mcpServers":{"genesis_peer":{"command":"<sys.executable>","args":["-P","-m","genesis.peers.facade","--lease-file","<absolute lease path>"]}}}
```

`-P` (Python's safe-path flag) is required: the peer runs in its working
directory, and `python -m` would otherwise put that directory first on
`sys.path`, importing any `genesis/` package found there before the facade or
its lease is validated.

The facade entry point and lease file are supplied by the later broker slice.
No token value belongs in argv or this configuration. The lease path is internal
and is not exposed in peer results. The configuration supplies no environment
overrides or additional servers.

Peer prompt/output/error prose is excluded from invoker logs and trace status
messages. Error redaction propagates to ancestor spans while exception types
and internal classification remain available to the coordinator. Exception
subclass attribute hooks cannot replace the original error or defeat redaction,
including when child tracing is disabled or its setup fails.

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
Failed scopes are collected after recursive control-group emptiness checks, so
a completed cleanup does not leave a failed unit name blocking a retry.
All invoker instances and event loops in the server process share ownership of
each segment name through cleanup. A duplicate is refused without stopping the
owner. An exceptional cleanup retains that process-local claim until restart;
this includes cancellation when completion cannot be established. This guard
does not coordinate independent launcher processes or grant peer permissions.

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

Launch reconciliation with checkout mutation: both public run paths register in flight, claim peer invocation ownership, then acquire checkout admission before roster and preflight reads. Admission is released on spawn or early failure before peer cleanup. Peer ownership and the in-flight unit remain active through descendant-aware drain. This preserves the ordinary checkout-launch fence while peer execution remains opt-in.
