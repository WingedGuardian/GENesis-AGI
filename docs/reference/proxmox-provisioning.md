# Proxmox provisioning — grow this VM's disk/RAM from the hypervisor

The last rung of Genesis's storage/RAM escalation ladder. When the container's
LVM-thin pool has no VG free extents (`vg_free=0`), autoextend can never fire and
no in-container action can fix it — the only cure is to grow the VM's virtual
disk at the hypervisor and absorb it into the pool. This subsystem lets Genesis
(or the guardian, offline) do that **from the host side, approval-gated, one
attempt at a time**.

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

Genesis-DOWN is not an edge case: a full pool → rootfs read-only → Genesis down
is the original outage, and the guardian growing the disk there **is** the
offline recovery. If Genesis flaps mid-approval, whoever is alive tries; a
failed read simply retries next cycle (rate-cap + repropose damper prevent
storms). The host gateway verbs and `__main__` provisioning verbs are therefore
**execute-only** (fresh re-check + execute + ledger, no Telegram gate); the
getUpdates gate lives only in the guardian's Genesis-DOWN path.

## Host setup (generalized — substitute your own values)

Replace `<NODE>` (PVE node name), `<VMID>` (this container's VM id), and choose
your own token secrets. Run on the PVE host:

```sh
# 1. A dedicated user + two roles (least privilege)
pveum user add genesis@pve
pveum role add GenesisAudit    -privs "Sys.Audit Datastore.Audit VM.Audit"
pveum role add GenesisProvision -privs "VM.Config.Disk VM.Config.Memory"

# 2. Audit role at / (read the node/storage/VM config)
pveum acl modify /                 -user genesis@pve -role GenesisAudit

# 3. Provision role scoped to THIS VM only
pveum acl modify /vms/<VMID>       -user genesis@pve -role GenesisProvision

# 4. Two privilege-separated tokens (privsep=1). Save the printed secrets.
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

### Validate (audit reads succeed, writes 403)

```sh
AUD='PVEAPIToken=genesis@pve!ro=<AUDIT_SECRET>'
PRV='PVEAPIToken=genesis@pve!provision=<PROVISION_SECRET>'
H=https://<PVE_HOST>:8006/api2/json

curl -sk -H "Authorization: $AUD" "$H/nodes/<NODE>/status"            | jq .data.memory
curl -sk -H "Authorization: $AUD" "$H/nodes/<NODE>/qemu/<VMID>/config" | jq '{cores,memory,scsi1}'
# Negative: the audit token must be REFUSED a write (expect 403)
curl -sk -X PUT -H "Authorization: $AUD" "$H/nodes/<NODE>/qemu/<VMID>/config" -d 'memory=99999'
```

> RAM headroom keys on `/nodes/<NODE>/status` → **`.memory.available`**, not
> `.memory.free`. On a busy host `free` is near-zero (Linux uses RAM as cache);
> gating on it would spuriously refuse every grow.

## Wiring on this install

1. **secrets.env** (container) — the ONLY secrets:
   ```
   PROXMOX_AUDIT_TOKEN=PVEAPIToken=genesis@pve!ro=<AUDIT_SECRET>
   PROXMOX_PROVISION_TOKEN=PVEAPIToken=genesis@pve!provision=<PROVISION_SECRET>
   ```
2. **Credential bridge** — the awareness tick propagates just those two keys to
   `<state_dir>/shared/guardian/proxmox_creds.env` (0600) for the host guardian
   to read. Host/node/vmid are non-secret config, not bridged.
3. **guardian.yaml** — fill the `provisioning:` block (`enabled: true`,
   `api_host`, `node`, `vmid`, `verify_tls`). See the commented template in
   `config/guardian.yaml`.
4. **guardian_remote.yaml** (container) — add `provisioning: true` so the
   sentinel offers the `host.resource_alloc` remediation for disk/RAM alarms.

## Operating it

- **Read capacity:** `provision_grow` is not needed — the guardian gateway
  `provision-status` verb (or `python -m genesis.guardian --provision-status`)
  prints host capacity JSON.
- **Grow disk (online):** the `provision_grow` MCP tool (`kind="disk"`) — asks
  you to APPROVE, then grows the disk and runs `storage-expand` to absorb it
  (pvresize → autoextend profile threshold 80 / percent 20 → verify
  `vg_free>0` + dmeventd monitoring). This is the structural `vg_free=0` fix.
- **Grow memory:** `provision_grow` (`kind="memory"`) grows *configured* RAM;
  it **takes effect only after a VM reboot** (hotplug is off on this install).
  The provision token deliberately lacks `VM.PowerMgmt` — power stays
  human/approved. Schedule the stop/start as a downtime window.
- **Rate cap / ledger:** executed mutations are recorded in
  `<state_dir>/provisioning/ledger.json`; the gate refuses once
  `max_actions_per_week` is reached. Autonomous pool-crit re-proposals are
  damped by `min_repropose_hours` (`proposal_state.json`).

## `verify_tls: false`

Most PVE hosts use a self-signed cert, so `verify_tls: false` is common on a
trusted LAN. It opens a MITM window on the token — acceptable on a controlled
LAN, but pin a CA and set `verify_tls: true` if the path is not trusted.
