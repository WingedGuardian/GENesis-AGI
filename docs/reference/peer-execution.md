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
`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` is forced again in the final spawn environment,
after routing and login fallback. Native CLI 2.1.280 controls loaded user,
ancestor and project canaries without the switch and excluded all three with
it, retaining facade calls and synthetic API-key/OAuth authentication. The
production-flags baseline did not reproduce the original reported user-memory
disclosure; the explicit switch supplies the tested boundary. Safe-mode would
disable the facade too. See the [vendor environment reference](https://code.claude.com/docs/en/env-vars).

The builder and final spawn gate also force `CLAUDE_CODE_DISABLE_ATTACHMENTS=1`.
Tool denial alone does not prevent the CLI from expanding prompt `@` references:
native 2.1.280 scratch controls disclosed outside-file contents or directory
names with that processing enabled, including relative and symlink references.
The switch keeps those references as plain text. Context must arrive explicitly
through authorized coordinator/facade paths. It does not disable facade tools.
The environment preserves HOME and native provider authentication, uses existing
roster credential routing, and excludes owner Genesis/GitHub credentials and
shell startup/session settings. Existing child environment pins still apply.
Owner-configured HTTP(S) proxy and bypass variables (uppercase/lowercase),
`SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS` are preserved. Peer requests cannot set
them. Launch resolves Claude and systemd-run to absolute executables before
applying peer cwd; empty/relative PATH entries, the peer directory and its
descendants/symlink aliases are excluded. Executables resolving into that
directory are refused. An absent PATH uses absolute system defaults; an
explicitly empty/unsafe PATH does not search cwd. Owner-configured relative
executable paths resolve against the parent cwd. There is no ambient fallback.

Each segment uses `genesis-peer-<32 lowercase hex>.scope` under the user systemd
manager, bound to `genesis-server.service` with `BindsTo` and `After`. RuntimeMaxSec
is the remaining deadline, capped at 7,200 seconds; stop timeout is ten seconds,
with control-group killing and SIGKILL enabled. There is no unscoped fallback.
Failed scopes are collected after recursive control-group emptiness checks, so
a completed cleanup does not leave a failed unit name blocking a retry.
All invoker instances and event loops in the server process share ownership of
each segment name through cleanup. A duplicate is refused without stopping the
owner. An exceptional cleanup retains that process-local claim until restart;
this includes cancellation of the cleanup task itself. Caller cancellation is
propagated after independently owned cleanup settles; a confirmed drain removes
the claim first. This guard
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

Peer stdout/stderr and result/error prose never enter account-error classification
or status notifications. Errors use fixed process messages; genuine structured
CLI rate-limit events retain fixed diagnostics. Dropped streams still raise the
unreplayable error. Empty-output callbacks retain their behavior but peer
callback failures omit exception traceback text. The generated scope property
set is supported by systemd 249; `RuntimeRandomizedExtraSec` is not supplied.
Native regression controls cover memory and attachment suppression with facade
calls in both output modes and both synthetic authentication paths.
Local native scope acceptance used systemd 255; an actual v249 manager remains
an unexercised compatibility limit.

Launch reconciliation with checkout mutation: both public run paths register in flight, claim peer invocation ownership, then acquire checkout admission before roster and preflight reads. Admission is released on spawn or early failure before peer cleanup. Peer ownership and the in-flight unit remain active through descendant-aware drain. This preserves the ordinary checkout-launch fence while peer execution remains opt-in.
