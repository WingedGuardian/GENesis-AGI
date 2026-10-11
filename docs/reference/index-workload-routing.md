# Indexing workload slice

`CODE_INTEL_WORKLOAD_SLICE=1` opts the existing queued indexing entrypoint into
`genesis-workload.slice` for its CBM and GitNexus batch scopes. The default is
`0`; other nonempty values refuse. Installer and bootstrap render the dormant
user slice with memory accounting and `ManagedOOMMemoryPressure=auto`. They do
not enable a new OOM monitor or set the routing option.

The scope probe and batch launch use the same slice. Each actual batch verifies
its kernel cgroup membership and exact generated scope name before executing
the indexer. Missing or unreadable membership, a different slice/scope, cgroup
v1, or an unavailable manager refuses the batch. Requested routing never uses
the GitNexus fallback. Refusals retain the existing per-leg queue outcome with
no indexing-attempt penalty; raw indexer failures remain failures. Existing
memory/zero-swap caps, CPU/IO settings, watchdog and child OOM priority remain.
CBM also retains destination-ancestor capacity admission.

Only explicitly launched indexing batches move. The queue supervisor,
foreground MCP readers, core, terminals and recovery sessions are not routed
by this option. The opt-in `CODE_INTEL_CBM_WORKER_BINARY` adapter places its
stock worker in the verified batch scope. The ordinary CBM CLI may delegate
indexing to its account daemon; routing that CLI alone does not prove its
daemon-side worker moved. Any future enforced workload policy must require
the verified worker adapter for CBM. Check actual worker membership rather
than assuming the caller's slice is the destination.

This is routing preparation, not a protection guarantee. `auto` does not exempt
descendants from a monitored ancestor. Existing `memory_resilience_apply`
continues its broad policy and existing posture facts keep their current
meaning. Core/recovery can remain candidates while a broad ancestor is
monitored, regardless of this slice's existence.

Before a separate OOM migration, verify in a disposable systemd 255 VM with
oomd in dry-run mode that bounded pressure actually selects owned expendable
descendants, with protected core/recovery/terminal and supervisor sentinels
outside. Include multiple jobs, restarts, grouped descendants, a no-pressure
control, and a deliberately broad ancestor monitor negative control. No
selection in the positive arm is inconclusive. Migration must reconcile all
effective monitors, update provisioning so upgrades cannot restore the broad
policy, adapt posture reporting, and verify live ancestry and rollback. This
narrows userspace pressure coverage for uncontained interactive workloads;
kernel OOM remains a separate mechanism.
