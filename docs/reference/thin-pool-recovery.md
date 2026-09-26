# Thin-pool pressure and recovery (LVM-thin storage pools)

On an Incus host whose storage pool is LVM-thin, the container's root disk is
a thin volume. A thin pool can promise more space than it has. When its DATA
or METADATA space reaches 100%, writes queue or fail, and the container's
filesystem goes read-only. Recovering from that means a stop, a filesystem
check, and a start. This page covers:

- how the Guardian keeps a pool from filling;
- what it will and won't do on its own;
- the manual recovery when a pool fills anyway.

## What fills a thin pool

Container data is the obvious consumer. The less obvious one is **snapshots**.
A thin snapshot costs almost nothing when it is taken. Every block the
container rewrites afterwards is copied, so a snapshot's cost grows with its
age at the container's churn rate, which is typically GBs per day.

**The measured failure this page exists for.** One install's daily "healthy"
snapshot could not be refreshed:

- Rotation creates the new snapshot before deleting the old one.
- LVM itself refuses to create a thin snapshot once the pool is past its
  autoextend threshold (80%) and cannot autoextend. After that, the Guardian's
  own pool gate refused as well.
- The week-old snapshot was never replaced. Its divergence grew about 2.3 GB/day
  until the pool reached 100%.
- LVM's autoextend was configured and fired, but it would not extend at all:
  it needs a full 20% step, and the volume group had only 3.9 GB free.
- CRITICAL storage alerts had been firing every six hours for four days.

## What the Guardian does automatically

It runs on every Guardian check, before the recovery cycle
(`src/genesis/guardian/pool_pressure.py`). Checks run every 30 seconds while
the container is fine. During an outage a single check can spend up to the
diagnosis timeout (an hour by default) in diagnosis, so relief then runs about
once an hour. That is ample against horizons of a day or two, but it is not
30 seconds.

1. **Measures and remembers.** It keeps a bounded history of data% and
   metadata%, converted to bytes, in `pool_history.jsonl` in the Guardian state
   directory: one sample per 5 minutes, 7 days.
2. **Derives the runway.** It computes, for data and for metadata alike:
   - the growth rate: the worst SUSTAINED growth over the last 2, 6, 24 and 72
     hours. The 72-hour window is what lets a job that writes a chunk once a
     day read as its daily average.
     Growth counts only when it shows in both halves of a window, so a one-off
     step never reads as a rate. That rules out a backup writing 2 GB in
     minutes, or the several-point metadata jump a new thin snapshot causes;
   - the worst burst in the last day: any 10-minute rise, or any rise between
     two consecutive readings up to 3 hours apart (ticks can be an hour apart
     during an outage). Longer gaps are left to the rate. The burst sets a
     free-space reserve of at least 3% of the pool for data and 10% of the
     metadata LV for metadata;
   - the hours until data or metadata is full.
3. **Relieves in two stages.** It frees at most one thing per check. After
   freeing something it waits one history interval (5 minutes) before freeing
   more, so the effect shows in a fresh measurement first. On btrfs, deleted
   space comes back asynchronously. It grows the pool at most once a day.
   - **Early** (runway < 48h):
     - grow the pool into volume-group space that LVM's autoextend cannot use
       (see below);
     - delete pre-recovery snapshots;
     - delete a healthy snapshot older than 48h.
   - **Urgent** (runway < 24h, or free space below the reserve): delete any
     Guardian snapshot. The rollback lifeline goes last.
4. **Rotates delete-first under pressure.** When all three hold:
   - the pool refuses the daily healthy snapshot;
   - the MEASURED runway says the pool is under pressure;
   - the current snapshot is over a day old;

   the Guardian deletes the old snapshot first (the oldest, if a failed
   rotation left two), then creates the new one. A pool that is merely FULL
   but stable keeps its aging snapshot: deleting it there frees next to
   nothing. A refused refresh retries in about an hour and alerts, at most
   once per `storage_pool.realert_hours`, where it used to be only a log line.
   A relief pass that keeps crashing alerts daily.

What it never does:

- Relief and delete-first rotation only ever delete snapshots with the exact
  names the Guardian generates (`guardian-YYYYmmdd-HHMMSS`, optionally followed
  by `-<label>`). (The older daily prune and rotation match the bare
  `guardian-` prefix, so do not give your own snapshots names starting with
  it.)
- It never deletes anything else. If the pool keeps filling after every
  Guardian snapshot is gone, it sends a CRITICAL alert saying something else is
  consuming the pool.
- It never grows a pool that has not opted in.

A pool that is NOT under pressure keeps its rollback lifeline, however long an
outage stops the Guardian from refreshing it. The lifeline is only refreshed
while the container is healthy, and an outage is exactly when rollback is
needed.

### The automatic extend (LVM only, opt-in)

The Guardian grows the thin pool only when **all** of these hold:

- the pool carries the `genesis-thinpool` LVM profile (host provisioning sets
  it; it is the install's opt-in to growing the pool into free space);
- data% is at or above the profile's autoextend threshold (80%);
- the volume group has LESS free space than one autoextend step (20% of the
  pool). At that point dmeventd's own autoextend refuses to act at all.

It extends by the free space minus a reserve kept for metadata growth
(512 MiB, or twice the metadata LV if that is larger), rounded down to whole
extents. It never uses `+100%FREE`. Each extend is recorded in the Guardian's
provisioning ledger and alerted. **A thin pool cannot be shrunk**, so this
permanently spends that volume-group space.

On btrfs, dir and other backends there is no extend. Relief there is by
deleting Guardian snapshots only. On a cloud VM, growing the disk is a
provider-console action (and usually a paid one), so the Guardian alerts
instead.

### Levers

In `guardian.yaml`. Defaults are in `src/genesis/guardian/config.py` and apply
with no overlay.

| key | default | meaning |
|---|---|---|
| `storage_pool.relief_mode` | `live` | `live` acts. `alert_only` only alerts: no relief action and no delete-first rotation. `off` also stops recording history. An invalid value becomes `alert_only`, with a warning. A bare `off` in YAML is read as a boolean and still means off. |
| `storage_pool.early_horizon_hours` | 48 | runway below which the early stage acts |
| `storage_pool.urgent_horizon_hours` | 24 | runway below which any Guardian snapshot may go |
| `storage_pool.burst_multiplier` | 2.0 | reserve = worst burst in the last day (10-minute rise, or a rise across a gap of up to 3h) × this |
| `storage_pool.min_reserve_pct` | 3.0 | data reserve floor (% of the pool) |
| `storage_pool.min_meta_reserve_pct` | 10.0 | metadata reserve floor (% of the metadata LV) |
| `storage_pool.extend_keep_free_mib` | 512 | volume-group space the extend never uses |
| `snapshots.lifeline_max_age_hours` | 48 | age at which the early stage may take the lifeline (≤ 0: never early) |

Environment kill switch: `GUARDIAN_POOL_RELIEF_DISABLED=1` forces `alert_only`,
which stops every automatic delete and extend this page describes.

Every value above is validated before relief may act (type, a finite range,
whole numbers where a count is expected). A bad value makes relief refuse to
act: for example `min_reserve_pct: 300`, `early_horizon_hours: "48h"`, or an
empty `snapshots.prefix`. It sends one warning a day, and the tier alerts keep
running. That daily warning, and every other relief alert, is sent only if its
"already alerted" stamp could be saved. An unwritable state dir therefore
produces no alert storm, just the tier alerts. Relief also stamps its settle and extend throttles
to disk BEFORE each change, and re-reads the pool right before acting. If the
stamp can't be written, or the pool is no longer the one it measured, it acts
on nothing.

## Reading the state on the host

```bash
# Pool data% / metadata%, size, profile. dmeventd autoextend needs the profile + monitoring.
sudo lvs -o lv_name,data_percent,metadata_percent,lv_size,lv_profile,seg_monitor <vg>
sudo vgs <vg>                                  # VFree = what an extend could use
incus snapshot list <container>                # guardian-* snapshots and their expiry
journalctl --user -u genesis-guardian.service --since -1d | grep -iE 'pool|snapshot'
sudo journalctl -t dmeventd --since -1d        # autoextend attempts ("Insufficient free space")
cat ~/.local/state/genesis-guardian/pool_history.jsonl | tail   # the Guardian's measured history
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
   - Either leave that free space for dmeventd's autoextend (it needs at least
     20% of the pool), or grow the pool by an explicit amount with
     `sudo lvextend -L +<N>G <vg>/<thinpool>`.
   - The Guardian's `storage-expand` path automates the pvresize and profile
     steps: see `docs/reference/proxmox-provisioning.md`.
3. Optionally grow the container's root disk:
   `incus config device set <container> root size=<new size>`. Check
   `df -h /` inside the container before and after. The disk only ever grows;
   it cannot be shrunk.
