# Thin-pool pressure and recovery (LVM-thin storage pools)

On an Incus host whose storage pool is LVM-thin, the container's root disk is
a thin volume. A thin pool can promise more space than it has. When its DATA
or METADATA space reaches 100%, writes queue or fail, and the container's
filesystem goes read-only. Recovering from that means a stop, a filesystem
check, and a start. This page covers:

- how the Guardian keeps its own snapshots from filling a pool;
- what it will and won't do on its own;
- the manual recovery when a pool fills anyway;
- adding headroom.

## What fills a thin pool

Container data is the obvious consumer. The less obvious one is **snapshots**.
A thin snapshot costs almost nothing when it is taken. Every block the
container rewrites afterwards is copied, so a snapshot's cost grows with its
age at the container's churn rate, which is typically GBs per day.

**The measured failure this page exists for.** One install's daily "healthy"
snapshot, the Guardian's offline rollback target, could not be refreshed:

- Rotation created the new snapshot before deleting the old one.
- LVM itself refuses to create a thin snapshot once the pool is past its
  autoextend threshold (80%) and cannot autoextend. After that, the Guardian's
  own pool gate (the `storage_pool` high tier) refused as well.
- So the old snapshot was never replaced. Its divergence grew about 2.3 GB/day
  for a week until the pool reached 100%.
- LVM's autoextend was configured and fired, but would not extend at all: it
  needs a full 20% step, and the volume group had only 3.9 GB free.
- CRITICAL storage alerts had been firing every six hours for four days. A
  refused refresh was only a log line.

## What the Guardian does automatically

Two mechanisms, both acting only on snapshots the Guardian itself created.

### 1. Delete-first rotation (stops the incident at its first day)

The daily healthy snapshot is still rotated create-then-delete, so an ordinary
failure never leaves the Guardian without a rollback target. The Guardian
deletes the old snapshot FIRST, then creates the new one, only when all of
these hold:

- the **pool** refused the create, on a real measurement: the Guardian's own
  gate on a measured, identified LVM thin pool (its tiers, or the relief
  reserve below), or LVM's own "free space in thin pool reached threshold". A failed probe (incus, `lvs` or `df`
  unreachable), a pool the Guardian cannot identify, and a generic
  "no space left" error (which can come from the host filesystem or the incus
  daemon) never count;
- the current healthy snapshot is at least 23 hours old;
- LVM **measures** that the healthy snapshots hold space nothing live maps:
  the pool's used bytes, minus what every live thin volume in the pool maps,
  must be at least 1 GiB (or 1% of the pool, if larger). That difference is a
  lower bound on what deleting the snapshots frees. A pool that is full of
  live container data keeps its snapshot, because new data grows the container
  and the pool alike and so never counts. Both figures come from one `lvs`
  read, and only a read that includes the running container's own volume
  counts. If any other volume in the pool cannot be read (an inactive one, or
  a snapshot you made yourself), there is no measurement and nothing is
  deleted. If the delete itself times out, it alerts that the rollback
  snapshot may be gone rather than retrying;
- relief is `live` (below), its configuration is valid, it has not deleted
  anything in the last 5 minutes, and its 5-minute settle stamp can be saved
  BEFORE the delete.

This needs LVM's per-volume view, so it runs on LVM-thin pools only. On btrfs
and dir pools delete-first never fires; pool relief (below) is the protection
there, and it covers every backend.

If a failed rotation left two healthy snapshots, the older one goes first.
Healthy snapshots are never evicted by the ordinary retention step before a
create, so a refused create cannot cost both of them. If
the create is still refused after the delete, there is no rollback target
until the pool recovers. The alert says so.

A refused refresh now retries in about an hour (without re-running the daily
prune) and sends a WARNING, at most once per `storage_pool.realert_hours`.

### 2. Pool relief (the backstop)

It runs on every Guardian check, before the recovery cycle
(`src/genesis/guardian/pool_relief.py`). When free DATA space is at or below
`min_reserve_pct` of the pool (default 3%), or free METADATA space is at or
below `min_meta_reserve_pct` of the metadata LV (default 10%), it deletes ONE
Guardian snapshot and sends a CRITICAL alert naming it. The order is:

1. pre-recovery snapshots, oldest first;
2. superseded healthy snapshots left behind by a failed rotation;
3. the current healthy snapshot (the rollback target) last.

Rollback (healthy) snapshots — 2 and 3 — are only ever deleted through one
check, and only when a rollback cannot need them: either a newer healthy
snapshot was just created (ordinary rotation), or THIS check's health probe
found the container healthy. So relief runs twice per check. The pass before
the health probe frees only pre-recovery snapshots, because a container that
failed since the last check still looks healthy there. The pass after the probe
may free rollback snapshots when the container is healthy, and when it is not,
keeps them and sends a CRITICAL alert ("rollback lifeline kept for recovery")
— it may be about to need one. "Healthy" here is the health probe's own
verdict on this check, not the Guardian's state: the state can read healthy
while the container is still down (after an automatic reset, or on unpause),
and that is exactly when a rollback may follow. The daily prune
and the retention step before a new snapshot never delete a rollback snapshot
at all: an older one is the fallback if the next refresh is refused.

If a delete fails (a busy volume, say), it re-lists the snapshots. If the
snapshot is gone anyway, that was this pass's delete. If the client timed out,
the daemon may still be finishing a slow delete, so it stops and alerts ("delete
outcome unknown") rather than deleting another. Otherwise it tries the next
snapshot in that order, still freeing at most one per pass. After deleting, it waits 5 minutes
before the next delete, so the effect shows in a fresh measurement first (btrfs
frees a deleted snapshot's space asynchronously). A delete-first rotation starts
the same 5-minute wait. The Guardian never takes a NEW snapshot while free
space is already inside the reserve. (A snapshot taken just above the reserve
can still be relief's target once the pool crosses it; that is the pressure
relief exists for.) A pass whose earlier delete failed never goes on to the
rollback snapshot: the failure may be the daemon still finishing a slow
delete.

Checks run every 30 seconds while the container is fine. During an outage a
single check can spend up to the diagnosis timeout (an hour by default) in
diagnosis, so relief then runs about once an hour. The container is down then,
so the pool is barely growing.

### What it never does

- It only ever deletes snapshots with the exact names the Guardian generates:
  `<prefix>YYYYmmdd-HHMMSS`, optionally followed by `-healthy` or
  `-pre-recovery` (the prefix is `guardian-` by default). The same rule governs
  the daily prune, the ordinary rotation and the choice of rollback target, so
  a snapshot you name `guardian-mine` by hand is never touched or restored.
  (Incus's own `snapshots.expiry`, which the Guardian sets on the container, is
  a separate matter: on some Incus versions it also expires hand-made
  snapshots. See the `snapshots.expiry` comment in `guardian.yaml`.)
- It never deletes anything else. If the pool is still short after every
  Guardian snapshot is gone, it sends a CRITICAL alert saying something else is
  consuming the pool (throttled to `storage_pool.realert_hours`).
- It never grows a pool. Adding space is an operator action (below).
- It never acts on a pool it cannot name or read: an unknown backend, a
  failed measurement, a measurement with no usage figures, or a volume group
  with several thin pools (whatever figures it shows) means no action. If that
  lasts an hour, it sends a WARNING ("Pool relief cannot act"), at most
  once a day.

### Levers

In `guardian.yaml`. Defaults are in `src/genesis/guardian/config.py` and apply
with no overlay.

| key | default | meaning |
|---|---|---|
| `storage_pool.relief_mode` | `live` | `live` acts. `alert_only` alerts instead of deleting, and also turns off delete-first rotation. `off` turns relief off. An invalid value becomes `alert_only`, with a daily warning. A bare `off` in YAML is read as a boolean and still means off. |
| `storage_pool.min_reserve_pct` | 3.0 | relief acts when free data space is at or below this % of the pool |
| `storage_pool.min_meta_reserve_pct` | 10.0 | relief acts when free metadata space is at or below this % of the metadata LV |

Environment kill switch: `GUARDIAN_POOL_RELIEF_DISABLED=1` forces `alert_only`,
which stops every automatic delete this page describes.

Relief refuses to act on a configuration it cannot trust: a reserve outside
0–50, `realert_hours` outside 0.1–720, a non-number, a
`storage_pool.enabled` that is not a real true/false (a quoted `"false"` is a
string), or an empty `snapshots.prefix`. It sends one warning a day instead. Every relief alert is
sent only if its "already alerted" stamp could be saved, so an unwritable state
directory produces no alert storm. Relief also saves its 5-minute settle stamp
BEFORE deleting, and re-reads the pool's identity right before deleting. If
the stamp cannot be saved, or the pool is no longer the one it measured, it
deletes nothing.

## Reading the state on the host

```bash
# Pool data% / metadata%, size, profile. dmeventd autoextend needs the profile + monitoring.
sudo lvs -o lv_name,data_percent,metadata_percent,lv_size,lv_profile,seg_monitor <vg>
sudo vgs <vg>                                  # VFree = what an extend could use
incus snapshot list <container>                # guardian-* snapshots and their expiry
journalctl --user -u genesis-guardian.service --since -1d | grep -iE 'pool|snapshot'
sudo journalctl -t dmeventd --since -1d        # autoextend attempts ("Insufficient free space")
sudo lvs -o lv_name,pool_lv,origin,data_percent,lv_size <vg>   # per volume: snapshots show a blank Data%
cat ~/.local/state/genesis-guardian/pool_relief_state.json   # relief's settle / alert stamps
```

## Manual recovery: the pool is at (or near) 100%

This is the order that recovered a live install. Run each step deliberately.

1. **Make room so I/O can resume.**
   - If the volume group has free space, extend the pool by an explicit size:
     `sudo lvextend -L +<N>G <vg>/<thinpool>`. Never use `+100%FREE`: it leaves
     no headroom for future autoextend.
   - If it has none, grow the VM disk first (below).
2. **Free the space a stale snapshot is holding.** Find the oldest `guardian-`
   snapshot (`incus snapshot list`) and remove it with
   `incus snapshot delete <container> <name>`. On a snapshot that has diverged a
   lot this can take minutes.
3. **Repair the filesystem the fill left behind.**
   - Stop the container: `incus stop <container>`. Add `--force` only if it
     hangs.
   - Check its volume offline: `sudo e2fsck -f /dev/<vg>/<container-lv>`. Take
     the LV name from `sudo lvs`.
   - Start it again: `incus start <container>`.
4. **Verify.**
   - Data% is well under 80%.
   - The container's services are up.
   - The Guardian journal shows ticks again. The next healthy snapshot will be
     taken on the daily cycle.

## Adding headroom: grow the VM disk, then the pool, then the container

Do these in this order: space for the pool first, then promises against it.
Growing the container's root disk first only adds promises the pool cannot keep.

1. Grow the VM's virtual disk in the hypervisor.
2. On the host, absorb it:
   - `sudo pvresize <pv>`; `sudo vgs` should now show `VFree`.
   - Either leave that free space for dmeventd's autoextend (it acts only when
     it can take a full step, 20% of the pool with Genesis's
     `genesis-thinpool` profile), or grow the pool by an explicit amount with
     `sudo lvextend -L +<N>G <vg>/<thinpool>`. A thin pool cannot be shrunk.
   - The Guardian's `storage-expand` path automates the pvresize and profile
     steps: see `docs/reference/proxmox-provisioning.md`.
3. Optionally grow the container's root disk:
   `incus config device set <container> root size=<new size>`. Check
   `df -h /` inside the container before and after. The disk only ever grows;
   it cannot be shrunk.

On btrfs and dir pools there is no thin pool to extend; relief works the same
way (deleting Guardian snapshots), measured from the pool's filesystem. Other
backends (zfs, ceph, …) are not measured: their pool path is not a mount of
the pool, so reading it would measure the host's own disk. Relief then does
nothing and says so ("Pool relief cannot act").
Free space there is what `df` reports as AVAILABLE: blocks a filesystem
reserves (ext4 keeps some for root) count as used, because the container cannot
write to them.
On a cloud VM, growing the disk is a provider-console action, usually a paid
one, so the Guardian alerts and leaves it to you.
