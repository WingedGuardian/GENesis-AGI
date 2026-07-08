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

# 5. Two privilege-separated tokens (privsep=1). Save the printed secrets.
pveum user token add genesis@pve ro       --privsep 1
pveum user token add genesis@pve provision --privsep 1
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
- **Rate cap / ledger:** executed mutations are recorded in
  `<state_dir>/provisioning/ledger.json`; the gate refuses once
  `max_actions_per_week` is reached. Autonomous pool-crit re-proposals are
  damped by `min_repropose_hours` (`proposal_state.json`).
- **`require_recent_backup`:** the backup-age check is not yet implemented for
  Proxmox (`newest_backup_age_days()` returns `None`), and the gate treats an
  unknown age as a refusal. So **setting `require_recent_backup: true` today
  refuses every grow.** Leave it `false` until the backup-age query lands; a
  grow is additive/grow-only and the container keeps a ≤24h healthy incus
  snapshot lifeline, so a corrupting rollback risk from the grow itself is nil.

## `verify_tls: false`

Most PVE hosts use a self-signed cert, so `verify_tls: false` is common on a
trusted LAN. It opens a MITM window on the token — acceptable on a controlled
LAN, but pin a CA and set `verify_tls: true` if the path is not trusted.
