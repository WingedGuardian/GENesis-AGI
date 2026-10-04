# Process discovery and termination ownership

The hourly `process_reaper` job is observation-only. Process names, age,
activity markers, terminal attachment and parent relationships are inspection
hints, not proof that a process is abandoned or safe to terminate. The job
never signals any discovered process or descendant, including the only
remaining recovery session.

Legacy `armed_by_operator` state and `GENESIS_REAPER_ARMED` values cannot
restore global signaling. Their presence produces a diagnostic warning.
`set_operator_armed(False)` clears a legacy state flag;
`set_operator_armed(True)` refuses. The existing state file is not rewritten
automatically. The historical `process_reaper_would_kill` observation type and
its fourteen-day TTL are retained; new records explicitly carry
`enforcement: observation-only` and `dry_run: true`. They are not cancellation
instructions.

Claude, Codex, OpenCode and browser helpers can appear in discovery results.
Filters remain heuristic and may miss processes. A missing discovery match
does not authorize deletion of an activity marker for a still-visible process.
Discovery failures are recorded through the existing job-health boundary.

This deliberately stops the former automatic cleanup of globally discovered
old browser and CLI trees. Such processes may continue to consume resources.
Explicitly owned job launchers retain their own cgroup limits, cancellation,
watchdogs, quarantine and durable outcomes. Cleanup requiring automation must
be attached to the job's owned launch boundary, with verified ownership and
identity; it cannot infer authority from this global inventory.

This change does not configure systemd-oomd or the kernel OOM killer. Either can
still terminate processes under its own policy. In particular, a root-owned
monitored ancestor does not honor user-owned descendant `ManagedOOMPreference`
attributes under systemd 255's ownership rules. `avoid` is a preference rather
than exclusion, and `omit` is not recursive. An OOM policy change requires
separate topology, candidate-selection, deployment and rollback verification.
