# Proxmox provisioning — grow this VM's disk/RAM from the hypervisor

The last rung of Genesis's storage/RAM escalation ladder. When the container's
storage pool is exhausted at a layer no in-container action can fix — an
LVM-thin pool with no VG free extents (`vg_free=0`), or a btrfs pool whose
backing LV has consumed its VG — the only cure is to grow the VM's virtual
disk at the hypervisor and absorb it into the pool. This subsystem lets Genesis
(or the guardian, offline) do that **from the host side, approval-gated, one
attempt at a time**.

## Supported pool substrates

`storage-expand` forks on the incus pool driver and absorbs accordingly:

| Substrate | Guest chain | Absorb steps |
|---|---|---|
| **LVM-thin** (driver `lvm`) | PVE disk → guest PV → VG → thin pool | rescan → `pvresize` → autoextend profile + dmeventd → verify `vg_free>0` (the data LV is NEVER extended — free extents ARE the autoextend headroom) |
| **btrfs-on-LVM** (driver `btrfs`, source a regular LV) | PVE disk → guest PV → VG → linear LV → btrfs | rescan → `pvresize` → `lvextend` the backing LV by an **explicit byte count** (clamped to `vg_free`) → `btrfs filesystem resize max <mount>` (online) → verify the fs grew |

The btrfs backing LV is resolved live from the pool's own mountpath
(`findmnt` → `lvs`), never from config — the absorb can only ever touch that
one LV, not siblings in the VG. Prefer a **whole-disk PV** as the grow target
(e.g. a dedicated `scsi1`): a partition-backed PV (`sda3`-style) needs a
partition grow before `pvresize` sees anything, which this flow does not do.
Any other driver (e.g. `dir`) is refused without touching anything.

> **Disabled by default.** Everything here is off unless you deliberately opt in
> (`provisioning.enabled: true` in `guardian.yaml` + the two API tokens in
> `secrets.env`). With it off, none of this code runs and no adapter is built.

## Threat model & guardrails

- **Two privilege-separated API tokens.** An **audit** token (read-only) does
  every capacity read; a **provision** token (write, scoped to *this VM's*
  disk + memory config only) does the two mutating calls. Reads can never
  write; the provision token can't touch any other VM or power state.
- **Every mutation is approval-gated** (Telegram/own-channel APPROVE/DENY) and
  **rate-capped** (`max_actions_per_week`, counting executed mutations).
- **Never-raise contract.** Adapter methods return typed failure forms, never
  raise — a provisioning bug cannot crash the guardian tick.
- **One attempt, verify-by-re-read, no auto-retry.** A grow is verified by
  re-reading config; an unverified mutation is reported CRITICAL and still
  counts against the rate cap. Grows are irreversible — the approval text says so.
- **Grow-only.** Shrink is refused before any API call.
- **`+100%FREE` is prohibited** in `storage-expand` (a runtime guard + a
  regression test): sizing the data LV to the whole VG is exactly what caused
  the original outage (no autoextend headroom).
- **Kill switch:** `GUARDIAN_PROVISIONING_ENABLED=0` overrides config to off.

## Approval ownership (one shared bot, two owners)

Telegram allows exactly one `getUpdates` consumer per bot token, and the main
Genesis bot polls continuously while it is up. So **whoever can read your reply
right now owns approval** — there is no second bot:

| Genesis state | Approval owner | How | Executes via |
|---|---|---|---|
| **UP** | the **container** | `provision_grow` MCP tool → `outreach` submit-and-wait (its own bot, zero contention) | host gateway execute verb |
| **DOWN** | the **guardian** | `getUpdates` on the shared token (uncontended — main bot is dead) | in-process execute-core |

The container (UP) path runs the approval-and-wait **in the genesis-server
process**, where the pipeline and the single Telegram reply-waiter live. A
standalone MCP subprocess (e.g. a Claude Code session) has no pipeline of its
own, so `provision_grow` / `outreach_send_and_wait` POST to the server's
localhost RPC routes (`/api/genesis/provision/grow`,
`/api/genesis/outreach/send_and_wait`) rather than failing — the server does the
ask and returns the result. Those routes are LAN-reachable via the dashboard
proxy like the rest of `/api/*`; `provision/grow` stays safe because it is
owner-APPROVE-gated before anything mutates.

Genesis-DOWN is not an edge case: a full pool → rootfs read-only → Genesis down
is the original outage, and the guardian growing the disk there **is** the
offline recovery. If Genesis flaps mid-approval, whoever is alive tries; a
failed read simply retries next cycle (rate-cap + repropose damper prevent
storms). The host gateway verbs and `__main__` provisioning verbs are therefore
**execute-only** (fresh re-check + execute + ledger, no Telegram gate); the
getUpdates gate lives only in the guardian's Genesis-DOWN path.

## Host setup (generalized — substitute your own values)

Replace `<NODE>` (PVE node name), `<VMID>` (this container's VM id), `<STORAGE>`
(the storage the VM's disks live on, e.g. `local-lvm`), and choose your own token
secrets. Run on the PVE host:

```sh
# 1. A dedicated user + two roles (least privilege)
pveum user add genesis@pve
pveum role add GenesisAudit    -privs "Sys.Audit Datastore.Audit VM.Audit"
# A disk grow ALLOCATES space on the datastore → Datastore.AllocateSpace is
# REQUIRED alongside VM.Config.Disk (without it the resize task 403s in the
# worker even though the PUT returns 200 — see "The ACL gotcha" below).
pveum role add GenesisProvision -privs "VM.Config.Disk VM.Config.Memory Datastore.AllocateSpace"

# 2. Audit role at / (read the node/storage/VM config)
pveum acl modify /                 -user genesis@pve -role GenesisAudit

# 3. Provision role scoped to THIS VM only
pveum acl modify /vms/<VMID>       -user genesis@pve -role GenesisProvision

# 4. Provision role ALSO on the storage (disk grow allocates space there)
pveum acl modify /storage/<STORAGE> -user genesis@pve -role GenesisProvision

# 5. Privilege-separated tokens (privsep=1). Save the printed secrets.
pveum user token add genesis@pve ro       --privsep 1
pveum user token add genesis@pve provision --privsep 1
# Optional third token — only if you enable vzdump backups (see "Backups"):
pveum user token add genesis@pve backup    --privsep 1
```

### ⚠ The ACL gotcha (this WILL bite you)

With privilege-separated tokens, effective permissions are
**`user ∩ token`**, and a **more-specific-path ACL REPLACES the propagated
one** — it does not add to it. So a token scoped at `/vms/<VMID>` sees *only*
the roles granted at `/vms/<VMID>`, not the ones inherited from `/`. You must
grant BOTH roles at the VM path for the audit token to read the VM's own config:

```sh
pveum acl modify /vms/<VMID> -user genesis@pve -role GenesisAudit
pveum acl modify /vms/<VMID> -token 'genesis@pve!ro'        -role GenesisAudit
pveum acl modify /vms/<VMID> -token 'genesis@pve!provision' -role GenesisProvision
```

The **same replacement rule bites again at the storage path**, in *both*
directions:

- The **provision token** needs `Datastore.AllocateSpace` at
  `/storage/<STORAGE>` (the role privilege from step 1 only takes effect where
  the ACL is granted). Without it, the resize PUT returns 200 but the async
  worker task fails `403 Permission check failed (/storage/<STORAGE>,
  Datastore.AllocateSpace)`.
- The moment you add ANY ACL at `/storage/<STORAGE>`, that path stops
  inheriting the `GenesisAudit` you granted at `/` — so the **audit token's
  storage read silently breaks** (avail flips to 0 / access denied) until you
  RE-grant `GenesisAudit` there too. Grant all three:

```sh
pveum acl modify /storage/<STORAGE> -token 'genesis@pve!provision' -role GenesisProvision
pveum acl modify /storage/<STORAGE> -user  genesis@pve             -role GenesisAudit
pveum acl modify /storage/<STORAGE> -token 'genesis@pve!ro'        -role GenesisAudit
```

Verify the provision token actually carries the privilege at the storage path:

```sh
pveum user token permissions genesis@pve provision /storage/<STORAGE> | grep -i AllocateSpace
```

### Validate (audit reads succeed, writes 403)

```sh
AUD='PVEAPIToken=genesis@pve!ro=<AUDIT_SECRET>'
PRV='PVEAPIToken=genesis@pve!provision=<PROVISION_SECRET>'
H=https://<PVE_HOST>:8006/api2/json

curl -sk -H "Authorization: $AUD" "$H/nodes/<NODE>/status"             | jq .data.memory
curl -sk -H "Authorization: $AUD" "$H/nodes/<NODE>/qemu/<VMID>/config" | jq '{cores,memory,scsi1}'
# Storage read MUST still return a non-zero avail after the /storage ACLs above
# (proves the GenesisAudit re-grant at /storage/<STORAGE> worked).
curl -sk -H "Authorization: $AUD" "$H/nodes/<NODE>/storage"           | jq '.data[] | select(.storage=="<STORAGE>") | .avail'
# Negative: the audit token must be REFUSED a write (expect 403)
curl -sk -X PUT -H "Authorization: $AUD" "$H/nodes/<NODE>/qemu/<VMID>/config" -d 'memory=99999'
```

> RAM headroom keys on `/nodes/<NODE>/status` → **`.memory.available`**, not
> `.memory.free`. On a busy host `free` is near-zero (Linux uses RAM as cache);
> gating on it would spuriously refuse every grow.

> **A resize is asynchronous.** `PUT .../qemu/<VMID>/resize` returns HTTP 200
> with a `UPID:` task string in `data`; the real work runs as a background task
> that can FAIL *after* the 200 (the missing-`Datastore.AllocateSpace` case
> above surfaces exactly this way). The adapter therefore polls
> `/nodes/<NODE>/tasks/<UPID>/status` until `status=stopped` and only reports
> success on `exitstatus=OK` — a failed task is reported as a failure, never as
> an unverified "slow" success.

## Wiring on this install

1. **secrets.env** (container) — the ONLY secrets:
   ```
   PROXMOX_AUDIT_TOKEN=PVEAPIToken=genesis@pve!ro=<AUDIT_SECRET>
   PROXMOX_PROVISION_TOKEN=PVEAPIToken=genesis@pve!provision=<PROVISION_SECRET>
   ```
2. **Credential bridge** — the awareness tick propagates just those two keys to
   `<state_dir>/shared/guardian/proxmox_creds.env` (0600) for the host guardian
   to read. Host/node/vmid are non-secret config, not bridged.
3. **provisioning config** — land the `provisioning` fields (`enabled: true`,
   `api_host`, `node`, `vmid`, `target_disk`, `storage`, `verify_tls`,
   `require_recent_backup`). Preferred: the audited, repeatable gateway verb
   ```
   configure-provisioning enabled=true api_host=<PVE> node=<node> vmid=<id> \
       target_disk=scsi1 storage=local-lvm verify_tls=false require_recent_backup=false
   ```
   which writes a `provisioning.local.yaml` **override in the guardian state_dir**
   (outside the git checkout, so `update.sh` redeploys never clobber it) that the
   config loader merges over `guardian.yaml`. `GUARDIAN_PROVISIONING_ENABLED=0`
   still force-disables it. (Alternatively, edit the `provisioning:` block in
   `guardian.yaml` directly — see the commented template in `config/guardian.yaml`.)
4. **guardian_remote.yaml** (container) — add `provisioning: true` so the
   sentinel offers the `host.resource_alloc` remediation for disk/RAM alarms.

## Operating it

- **Read capacity:** `provision_grow` is not needed — the guardian gateway
  `provision-status` verb (or `python -m genesis.guardian --provision-status`)
  prints host capacity JSON.
- **Grow disk (online):** the `provision_grow` MCP tool (`kind="disk"`) — asks
  you to APPROVE, then grows the disk and runs `storage-expand` to absorb it
  per substrate (LVM-thin: pvresize → autoextend profile threshold 80 /
  percent 20 → verify `vg_free>0` + dmeventd monitoring; btrfs-on-LVM:
  pvresize → lvextend by the approved amount → `btrfs filesystem resize max` →
  verify the fs grew). This is the structural pool-exhaustion fix.
- **Grow memory:** `provision_grow` (`kind="memory"`) grows *configured* RAM;
  it **takes effect only after a VM reboot** (hotplug is off on this install).
  The provision token deliberately lacks `VM.PowerMgmt` — power stays
  human/approved. Schedule the stop/start as a downtime window.
- **Grow the CONTAINER (local, no Proxmox token):** `provision_grow`
  (`kind="root"`) grows the container root volume to `<gib>` GB total — incus
  resizes the thin LV + filesystem ONLINE, no restart (`guardian/grow_capacity.py`,
  grow-only, refused if the thin pool is near-full). `provision_grow`
  (`kind="limits"`) raises the container cgroup caps (`<mib>` MiB / `<cpu>` cores,
  grow-only, applied live, memory hard-capped below host `MemTotal−reserve`). The
  **Phase-C RAM completion**: after a `kind="memory"` VM grow + reboot, run
  `kind="limits"` so the grown RAM actually reaches the container.
- **Rate cap / ledger:** executed mutations are recorded in
  `<state_dir>/provisioning/ledger.json`; the gate refuses once
  `max_actions_per_week` is reached. Autonomous pool-crit re-proposals are
  damped by `min_repropose_hours` (`proposal_state.json`).
- **`require_recent_backup`:** the backup-age query IS implemented
  (`newest_backup_age_days()` reads the backup storage's `content=backup`
  listing with the audit token — no extra privileges). The gate still treats an
  unknown age as a refusal, so only flip this to `true` once a first verified
  vzdump exists (see "Backups (vzdump)" below); with the JIT chain in place a
  stale backup then turns a grow proposal into a backup→verify→grow chain
  instead of a dead end.

## Backups (vzdump) — two-phase, JIT + rotation

Genesis can take a hypervisor backup of the host VM (`vzdump`), which both
creates a real VM-level restore point and unblocks the grow gate's
`require_recent_backup` check. Design properties:

- **Just-in-time, not scheduled.** A backup is proposed as the precondition
  step of a grow (when the gate would refuse on backup age, the grow proposal
  becomes a backup→verify→grow **chain under one approval**, with the time gap
  and auto-execute disclosed in the approval text) or on explicit ask
  (`provision_vzdump` MCP tool). There is no cron and no recurring proposal.
- **Two-phase.** `provision-vzdump` only STARTS the task and returns the PVE
  UPID (a full-VM dump runs for tens of minutes+); `provision-vzdump-status
  [UPID]` is a single verify probe (no arg = resume the latest in-flight
  backup — restart-safe). The start is ledgered immediately: the row is the
  rate-cap entry (`max_backups_per_week`, separate from the grow budget), the
  in-flight latch, and the resume handle.
- **Rotation is the only cleanup.** After a VERIFIED new backup, old ones are
  pruned to `backup_keep_last` via the standalone `prunebackups` endpoint
  (owner semantics — `Datastore.AllocateSpace` + `VM.Backup` suffice; the
  inline vzdump `prune-backups` parameter would demand `Datastore.Allocate`
  and is deliberately NOT used). There is no delete verb. Note the store
  transiently holds `keep_last + 1` backups until rotation runs — size the
  storage (or `backup_size_multiplier`) accordingly.
- **Consistency class:** without a QEMU guest agent the backup is
  crash-consistent (like a power cut — journaled fs + SQLite WAL recover);
  with an agent, filesystem-consistent. The gate report shows which one you
  are buying. **A backup you have never restored is unproven DR** — restore is
  deliberately NOT implemented in this slice (it is a destructive verb with
  its own review); treat these backups as a rollback point of last resort
  until a restore path ships.

Config fields (all generic; land per-install values via the audited
`configure-provisioning` gateway verb): `backup_storage` (empty = same as
`storage`), `backup_keep_last` (default 2), `max_backups_per_week` (default
2), `backup_size_multiplier` (default 1.0 — worst-case incompressible
estimate against the backup storage's free space), `vzdump_timeout_s`
(default 7200 — the verify poller's wall bound; the backup itself is never
killed).

### The backup token

The third token keeps the privsep model honest: `provision` stays grow-only
(no `VM.Backup`), and `backup` cannot resize anything.

```sh
# Role: backup + the space to write it
pveum role add GenesisBackup -privs "VM.Backup,Datastore.AllocateSpace"

# ACLs — the same replacement gotcha as above applies: granting ANY ACL on a
# path replaces what that path inherited, so re-grant audit alongside.
pveum acl modify /vms/<VMID>              -token 'genesis@pve!backup' -role GenesisBackup
pveum acl modify /storage/<BACKUP_STORE>  -token 'genesis@pve!backup' -role GenesisBackup
pveum acl modify /storage/<BACKUP_STORE>  -user  genesis@pve          -role GenesisAudit
pveum acl modify /storage/<BACKUP_STORE>  -token 'genesis@pve!ro'     -role GenesisAudit
```

Land the secret as `PROXMOX_BACKUP_TOKEN` next to the other two (container
`secrets.env` → credential bridge, or the host guardian's own secrets file).
An absent backup token degrades safely: backup verbs refuse pre-flight,
grows are unaffected.

Validate — positive AND negative (the boundary only exists if you probe it):

```sh
BAK='PVEAPIToken=genesis@pve!backup=<BACKUP_SECRET>'
# Positive: token sees its own permissions on the two granted paths
pveum user token permissions genesis@pve backup /vms/<VMID>            | grep -i VM.Backup
pveum user token permissions genesis@pve backup /storage/<BACKUP_STORE> | grep -i AllocateSpace
# Negative: the backup token must be REFUSED a resize (expect 403)
curl -sk -X PUT -H "Authorization: $BAK" "$H/nodes/<NODE>/qemu/<VMID>/resize" -d 'disk=scsi1' -d 'size=+1G'
# Negative: the provision token must be REFUSED a vzdump (expect 403)
curl -sk -X POST -H "Authorization: $PRV" "$H/nodes/<NODE>/vzdump" -d 'vmid=<VMID>' -d 'storage=<BACKUP_STORE>'
```

## `verify_tls: false`

Most PVE hosts use a self-signed cert, so `verify_tls: false` is common on a
trusted LAN. It opens a MITM window on the token — acceptable on a controlled
LAN, but pin a CA and set `verify_tls: true` if the path is not trusted.
