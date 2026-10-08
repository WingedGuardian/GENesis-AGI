# Stock Codebase worker boundary

Codebase v0.11 ordinary CLI indexing delegates to the account daemon. The daemon
launches the physical worker with its own cgroup, OOM priority and native memory
budget. Capping the one-shot CLI therefore does not contain that worker when a
query daemon already exists.

## Opt-in queued adapter

Immutable managed settings select the pinned executable for the stock internal
worker adapter in `scripts/lib/code_intel_cbm_worker.py`. The existing
`code_intel_index.sh` entrypoint invokes that fixed adapter with explicit managed
configuration inside the admitted batch scope. PATH and a worker-binary override
do not replace this authority.
The durable queue, worktree skip, single-flight locks, physical resource caps,
child admission, OOM priority 1000 and cgroup watchdog continue to apply.

The adapter supports exactly the v0.11.0 Linux x86_64 portable executable with
SHA-256 `ce11c141431aeadd788506c3a7e6942db8fd438dec369d0707a39ec9fd8c6510`.
It hashes and executes the same opened inode. Unknown builds refuse; there is no
fallback through PATH to an ordinary daemon-backed CLI. This uses internal ABI,
not a stable public CLI contract. Re-run acceptance before changing the pin.

Configure the cache and runtime directories through managed setup. Query clients,
the query daemon and the worker use the same configured stock cache and IPC namespace. The
adapter preserves native build-cohort admission, project mutation locks,
maintenance cancellation, parent-death handling and publication. Its cooperative
worker budget is three quarters of the admitted physical cap; the physical cap
is unchanged. The remaining quarter is headroom for other charged memory.

A clean native process exit is insufficient: stock workers also return zero for
MCP tool errors. The adapter reads a bounded response and propagates tool or
transport failure as a failed attempt. A repeat capacity refusal uses the
existing refusal marker, preserving the queued request without charging an
attempt. Crashes fail the attempt; the upstream supervisor's file skip/retry
mechanism is not reproduced. The existing cgroup watchdog controls the physical
worker and all its descendants.

## Activation and verification

This change does not install or enable a query daemon, alter the Codebase
sentinel/shim, or register new MCP clients. Those require their own managed
lifecycle acceptance. Leave the production sentinel intact during private tests.

Use the existing durable queue and idle-gated runner. Never invoke this helper
as an alternative index entrypoint. The helper itself refuses without valid
batch-scope admission and a nonmaximum supervisor OOM adjustment; only its native
child is promoted to 1000. For a shared deployment, include the
full query-daemon and aggregate frontend ceilings in the capacity envelope,
plus the existing sibling reserve; current usage alone is insufficient.

Private acceptance measured a 12 GiB zero-swap batch ceiling, a separate 2 GiB
query daemon, a 2 GiB aggregate frontend ceiling and 2 GiB sibling reserve.
The physical worker stayed in the batch scope with OOM priority 1000 and a
9 GiB native budget. Eight indexed readers succeeded during a changed-repository
full publication. A fresh reader saw the new symbol. Scope cancellation retained
the published graph, left the query daemon alive and durably charged one failed
attempt. A subsequent queued index succeeded. A separately preregistered 8 GiB cap / 6 GiB native budget also completed a
changed-repository full index with persistence enabled in 128.6 seconds while
eight persistent readers remained active; sampled scope peak including persistence export was 1.33 GiB.
That cap leaves about six times the largest measured job charge. It was derived
from Codebase measurements; the earlier 797 MiB reading covered the indexing
phase rather than the complete persistence job. The artifact roundtrip retained a healthy SQLite
graph and the newly indexed symbol. Killing only the adapter preserved the graph,
cleaned up the worker and retained a failed attempt. Resuming that same request
with fault injection removed consumed it successfully without resetting its
attempt count. These are measurements for the pinned build and tested repository,
not a completed long-run acceptance claim.

## Rollback

Keep the sentinel armed and disable the managed query service through its
operator lifecycle. Managed indexing then refuses and preserves queued work;
there is no fallback to the daemon-backed CLI. Preserve the last usable graph,
its artifact and failed requests when quarantining a run. Effectively sandboxed
custom write roots require the operator-owned runner allowlist documented in
`codebase-managed.md`. Persistent artifact export also requires writable native
snapshot scratch at `/tmp`; an operator-owned disk-directory bind can supply it
without changing the host's shared temporary directory or overriding `TMPDIR`.
Direct shell success does not prove timer writability.
