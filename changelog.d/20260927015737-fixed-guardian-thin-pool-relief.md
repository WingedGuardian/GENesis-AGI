- **The Guardian now keeps its own snapshots from filling an LVM-thin storage
  pool, instead of only warning about it.** A thin snapshot costs almost
  nothing when it is taken, then grows with every block the container
  rewrites. The daily "healthy" rollback snapshot was replaced create-first,
  and the replacement can be refused once the pool is past LVM's autoextend
  threshold. When that happened the old snapshot stayed and kept growing. On
  one install it grew for a week until the pool hit 100% and the container's
  filesystem went read-only, while CRITICAL alerts fired every six hours. (An
  earlier entry said this snapshot is "never more than a day old"; that held
  only while the replacement succeeded.) Now:
  - On LVM-thin pools, when the pool refuses the new snapshot on a real
    measurement, the old one is over a day old, and LVM shows the snapshots
    hold at least the larger of 1 GiB and 1% of the pool that no live volume
    maps, the Guardian deletes the old snapshot first, then creates the new
    one. A pool that is full of live container data keeps its rollback
    snapshot, since deleting it would free almost nothing. A failed probe never
    counts as a refusal. On btrfs and dir pools this step does not run; the
    backstop below protects them. A refused refresh retries within about an hour and sends an
    alert, throttled rather than hourly; it used to be only a log line.
  - A backstop runs every tick: when free data space is at or below 3% of the
    pool, or free metadata space at or below 10%, it deletes one Guardian
    snapshot (pre-recovery snapshots first, the rollback snapshot last), waits
    five minutes for a fresh measurement, and alerts on each delete. If no
    Guardian snapshot is left, it alerts that something else is filling the
    pool. No new Guardian snapshot is taken while free space is inside that
    reserve. A rollback snapshot is only ever deleted when a newer one was just
    created, or when this check's health probe found the container healthy;
    while it is not, the backstop keeps them and alerts. The daily prune never
    deletes a rollback snapshot. A delete whose outcome is unknown (the client timed out) stops
    the pass instead of moving on to another snapshot. If relief cannot measure or identify the pool for an hour, it
    says so in a daily warning rather than going quiet.
  - Every Guardian path that deletes or restores a snapshot (relief,
    rotation, the daily prune, the rollback target) now matches only the exact
    names the Guardian generates (`guardian-YYYYmmdd-HHMMSS`, optionally
    `-healthy` or `-pre-recovery`). Previously the prune and rotation matched
    any name starting with `guardian-`, so a hand-made `guardian-…-healthy`
    snapshot could be deleted or chosen as the rollback target. Relief never
    acts on a pool it cannot identify, and nothing here grows the pool.
  - The pool is identified by what incus declares (`lvm.vg_name`,
    `lvm.thinpool_name`), not inferred. A backend that cannot be determined,
    thick LVM, and backends whose pool path is not a mount (zfs, ceph) now read
    as "not measured" rather than falling back to `df`, which measured the host
    filesystem instead of the pool. On btrfs and dir pools, free space is what
    `df` reports as available, so reserved blocks count as used.
  - Levers: `storage_pool.relief_mode` (`live` / `alert_only` / `off`) and the
    two reserves in `guardian.yaml`, plus the `GUARDIAN_POOL_RELIEF_DISABLED=1`
    kill switch, which stops every automatic delete. Recovery runbook:
    `docs/reference/thin-pool-recovery.md`.
