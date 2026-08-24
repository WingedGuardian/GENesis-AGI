# Tailscale SSH Access to Your Genesis Fleet

How to reach a Genesis host (and its persistent Claude Code tmux slots) over
Tailscale from any client device — and how to keep that access rock-solid across
reboots on Windows clients with a busy network stack.

## What this gives you

Each Genesis host exposes numbered tmux **slots** and a **lobby** landing session
over SSH. From a laptop or phone you type `ssh <host>-1` to land in a specific
persistent session, or `ssh <host>-lobby` to open a picker of every live slot.
The sessions live in tmux on the box, so a client reboot never kills them — one
reconnect brings the whole fleet back in the state you left it.

## Setup

On each Genesis host, generate the client config snippet and paste it into your
client's `~/.ssh/config` (Windows: `C:\Users\<you>\.ssh\config`):

```bash
./scripts/generate-ssh-config.sh
```

Copy the printed block into `~/.ssh/config` on every client device, **replacing**
any earlier Genesis block for that host (don't append a second one — SSH uses the
first matching block, so a stale one would win). Then:

```
ssh <host>-1        # a specific slot (any positive integer)
ssh <host>-lobby    # the session picker → pick any live slot
```

Windows one-click: make a shortcut whose target is `wt.exe ssh <host>-lobby`
(or `ssh.exe <host>-lobby`) and pin it to the taskbar.

## Why the aliases are keyed on the Tailscale IP, not the MagicDNS name

`generate-ssh-config.sh` writes each `HostName` as the host's **stable Tailscale
IPv4**, not its `*.ts.net` MagicDNS name. A Tailscale IP is pinned per device and
routes without the client's DNS resolver, so the config keeps working even when
MagicDNS is disrupted on the client. This matters because MagicDNS is the fragile
link on a busy Windows client (see below). Re-run the generator if a host's
Tailscale IP ever changes (device removed and re-added to the tailnet).

## Troubleshooting: SSH breaks after a reboot (Windows)

**Symptom.** Right after a reboot/login, `ssh <host>-N` times out and/or plain
tailnet names won't resolve (`ssh user@<name>` → "could not resolve hostname").
After a while it starts working again on its own.

**Cause.** Two things degrade for a window after boot, and both self-heal once
Tailscale fully settles:

1. **Cold tunnels.** Tailscale hasn't yet established the peer connections, so
   even an IP-keyed alias times out until the tunnel warms.
2. **MagicDNS applied late.** On a client with a **contended DNS stack** —
   several DNS-managing VPNs installed, a large hosts-file ad/malware blocker,
   and/or manually pinned DNS servers — Tailscale's MagicDNS write repeatedly
   loses a race for the Windows DNS configuration at boot. Its health then shows
   `dns-set-os-config-failed: The process cannot access the file because it is
   being used by another process`, and tailnet **names** don't resolve until a
   write finally wins.

This is a client-side environmental race, not a Genesis or Tailscale defect — it
tends to appear only on machines carrying a lot of network software.

### Fix 1 — IP-key every alias (removes the DNS dependency)

The generator already IP-keys the slot and lobby aliases. If you add **your own**
hosts to `~/.ssh/config`, key those on the Tailscale IP too, so none of your SSH
depends on MagicDNS:

```
# Instead of:  HostName my-box            (needs MagicDNS)
# Use:         HostName 100.x.y.z         (the peer's Tailscale IP)
```

Find a peer's Tailscale IP with `tailscale ip -4 <peer>` or `tailscale status`.
With every alias IP-keyed, the MagicDNS flap can no longer affect your SSH.

### Fix 2 — Install the boot self-heal (closes the cold-tunnel window)

`scripts/tailscale-ssh-selfheal.ps1` installs a scheduled task that, a short
delay after each login, re-applies MagicDNS and pings every online tailnet peer
to warm the tunnels — so the fleet is reachable within a few seconds of login
instead of after an unpredictable settle. Run it once, in an **elevated**
PowerShell on the client:

```powershell
# from the repo's scripts\ directory, elevated:
.\tailscale-ssh-selfheal.ps1                 # install (default 45s post-login delay)
.\tailscale-ssh-selfheal.ps1 -DelaySeconds 30
.\tailscale-ssh-selfheal.ps1 -Uninstall      # remove it
```

It derives peers live from `tailscale status`, so it hardcodes nothing about your
tailnet. Logs to `C:\ProgramData\GenesisNet\<TaskName>.log` (by default
`GenesisTailscaleSelfHeal.log` — the installer prints the exact path on the `Log:`
line). Test without rebooting: `Start-ScheduledTask -TaskName GenesisTailscaleSelfHeal`,
then read that log.

### The battery gotcha (why a hand-rolled task may silently do nothing)

On a **laptop**, Windows scheduled tasks default to *"don't start on battery
power"* — a task created with a bare `schtasks /create` will sit **Queued** and
never run while unplugged, with no error. The installer avoids this by using
`Register-ScheduledTask` with `-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries`.
If you build your own task, set those or it will no-op on battery.

### The cosmetic health warning

Even after the fixes, Tailscale may still *show* `dns-set-os-config-failed`
flapping if you keep the contended DNS stack (multiple VPNs, a big hosts blocker,
pinned DNS). That warning no longer affects your SSH once every alias is
IP-keyed — nothing you type relies on MagicDNS. If you want it gone at the root,
reduce the contention: remove VPNs you don't use, move ad-blocking off the
hosts file (e.g. to a DNS resolver), or let Tailscale manage DNS.

## Reverting

`generate-ssh-config.sh` only prints a snippet — you paste it, so reverting is
editing your own `~/.ssh/config`. The self-heal task is removed with
`.\tailscale-ssh-selfheal.ps1 -Uninstall`.
