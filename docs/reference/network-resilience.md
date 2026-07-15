# Network Resilience — KeepConfiguration + a self-healing networkd watchdog

## The invariant

**A systemd-networkd failure must degrade into "address retained, renewals
paused, daemon auto-restarted" — never into a container that silently falls off
the network and stays there until a human runs `systemctl restart` hours
later.**

Genesis is a memory-heavy system, and its worst incidents cluster: the same
pressure spikes that wedge memory also make the kernel's rtnetlink socket time
out. When that happens mid-DHCP-renewal, stock networkd drops the lease, tears
down the address, and the box is gone. One 2026-07 incident series produced
exactly this three times in three days — each time from an unrelated pressure
event, each time recovered by hand.

The fingerprint, if you're diagnosing it live:

- `journalctl -u systemd-networkd` shows
  `<iface>: Could not set route: Connection timed out` followed by
  `<iface>: Failed` (often minutes apart, under load).
- `networkctl status <iface>` shows **`State: routable (failed)`** —
  `AdministrativeState=failed` (link SETUP failed) while `OperationalState`
  stays `routable` (the address is still held, by KeepConfiguration).
- Pre-fix only: the address then disappears (`ip addr` empty on the iface) and
  connectivity dies until networkd is restarted.

The administrative-vs-operational split is the key tell: dashboards that show
only "routable" look healthy while the link is actually wedged. Check
`networkctl status`, not just reachability.

## What Genesis sets up

`scripts/lib/network_resilience.sh` runs from `bootstrap.sh` on fresh installs
and from every `update.sh` (existing installs retrofit automatically). It is
idempotent — unchanged files produce no reload/restart churn — and **adaptive:
the protected interface and its `.network` unit are discovered live (via
`ip route` + `networkctl status`), never hardcoded**, so the same code fits any
install.

**Layer 1 — the address survives the failure.**
`/etc/systemd/network/<iface-unit>.network.d/genesis-keep-config.conf` sets
`KeepConfiguration=true`. `true` is the superset (`yes ⊃ dhcp ⊃ dhcp-on-stop`,
per systemd.network(5)): the address and routes provided by DHCP are **never**
dropped even if the lease expires or the daemon stops, and static/foreign
config is kept too. It is exactly what netplan `critical: true` renders to, so
one drop-in delivers the full protection. Written under `/etc` (not the
`/run`-rendered unit), so it survives `netplan apply` regeneration.

*Cost, by design:* re-addressing that interface then requires a full networkd
restart (or manual flush) — a reconfigure request alone won't tear down kept
config (networkd logs "considered critical, ignoring request to reconfigure").
On a server whose whole job depends on the connection, that trade is correct.

**Layer 2 — the daemon heals itself.**
`genesis-network-watchdog.timer` runs `/usr/local/lib/genesis/network-watchdog.sh`
as root every ~2 minutes. It restarts systemd-networkd when any of:

1. the daemon is **inactive** (but not `masked` — a mask is operator intent);
2. a managed link is in **`AdministrativeState=failed`** (the live
   fingerprint); or
3. there is **no IPv4 default route**.

The restart is address-preserving because of Layer 1, so it heals the wedge
without a connectivity blip. Safety rails: a **2-minute grace window** (skip a
networkd that just (re)started, so we never fight a settling daemon) and a
**10-minute rate limit** (a persistent fault logs loudly each tick instead of
flap-restarting). The healthy path exits silently — no per-tick journal spam.

Graceful degradation: no systemd, no `networkctl`, systemd-networkd not the
active manager (NetworkManager hosts), or no non-interactive sudo each produce
a one-line skip note and never a failure.

## How the body schema surfaces it

The infrastructure profile (`INFRASTRUCTURE.md`, `infra_profile` package)
records the posture as facts, so the annotation layer flags an unprotected
install on its own:

| Key | Kind | Healthy | Defect |
|---|---|---|---|
| `networkd_keep_configuration` | fact | `true` | `false` |
| `network_watchdog_installed` | fact | `true` | `false` |
| `watchdog` (heal telemetry) | metric | rare/zero heals | frequent heals |

`watchdog` is a **metric** (never hashed): the watchdog rewrites
`/run/genesis-network-watchdog.json` every run
(`last_check`/`last_heal`/`last_trigger`/`heal_count`/`last_action`), so heals
show up in the rendered doc and dashboard instead of being buried in root logs
— directly closing the "nothing noticed for 15 hours" half of the incident.

## Notes

- **Shared root cause with memory resilience.** The rtnetlink timeouts that
  wedge networkd are driven by the same pressure spikes that
  `docs/reference/memory-resilience.md` addresses. Fixing memory pressure
  reduces how often networkd is stressed; this layer covers what happens when
  it is stressed anyway. Treat them as two halves of one resilience story.
- **These are last lines of defense, not a network manager.** If the watchdog
  ever heals repeatedly (check the `watchdog` metric / `journalctl -u
  genesis-network-watchdog`), a link is genuinely failing — investigate the
  cause, don't lengthen the interval to hide it.
- **Assumes an IPv4 default route** (the Genesis norm). On an IPv6-only or
  route-less-by-design install, Layer 1 skips cleanly (nothing to protect) and
  the watchdog's no-route trigger would be a false positive — adjust the
  triggers before deploying there.
- **Host networkd is out of scope.** The guardian owns host-VM reachability;
  no host networkd incident has been observed. This covers the container plane
  where the incidents happened.
