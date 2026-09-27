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

**Layer 3 — a stuck Tailscale tunnel heals itself.**
The same watchdog run also checks Tailscale, independently of the networkd
checks (neither one's failure or rate limit gates the other). The failure it
targets was observed on a live install. The address and the network path to a
peer were both fine, but that peer stopped completing WireGuard handshakes: it
kept sending its replies through a relay (DERP) region this node had already
moved away from. Its network map did not pick up the move for about five
minutes, and its log showed `derp-<n> does not know about peer [...]` on every
reply. Every session over that tunnel timed out, and nothing else on the box
looked wrong.

The fingerprint, which is also the watchdog's trigger (all three must hold):

- the peer is `Active` in `tailscale status --json` (traffic is wanted) and its
  `LastHandshake` is non-zero and older than `NETWD_TS_STALE_SEC` (default
  300s: WireGuard stops using a session after 180s without a new handshake,
  and 300s spans at least two 2-minute watchdog runs);
- `tailscale ping --tsmp <peer>` (through the tunnel) gets no reply; and
- `tailscale ping <peer>` (discovery, no `--tsmp`) does reply, so the peer is
  reachable. A peer that is simply offline fails both pings and is left alone,
  because restarting the local daemon cannot bring it back.

The heal is `systemctl restart tailscaled`. In the observed incident the node
came back with a new disco key (the node key persists), and the coordination
server pushed the peer a fresh network map carrying the current relay. The
cost is that **every Tailscale SSH session on the box drops**, including
healthy ones from other peers. Sessions inside tmux survive and reattach. So it
is rate-limited to once per `NETWD_TS_RATE_LIMIT_SEC` (default 3600s), and the
limit is armed before the attempt, because a restart that fails has still
dropped every session. A failed restart is not retried: it queues its own alert
saying tailscaled may be down, because later runs see the daemon stopped and,
by design, never start a stopped tailscaled. Every tailscale call is bounded
(`NETWD_TS_CALL_TIMEOUT`, `NETWD_TS_RESTART_TIMEOUT`), so a hung local API
cannot wedge the oneshot unit and stop the networkd checks with it. Output from
`tailscale status` that cannot be read as a peer map is recorded as
`status-unparseable`, never as healthy. The check skips cleanly where Tailscale
is not installed or not running.

Before anything changes, each detection records its evidence: the full
`tailscale status --json` goes to
`/run/genesis-network-watchdog-tailscale-status.json` (root-only, since it
names every node on the tailnet), and a summary goes under `tailscale` in the
telemetry file (peer, handshake age, both relay regions, direct address, ping
results). It then queues a `warning` alert in the owning user's
`~/.genesis/alerts/queue`, which the awareness tick delivers to Telegram. The
watchdog runs as root, so the installer writes that queue's path into the
service unit (`NETWD_ALERT_QUEUE`), and each entry is chowned to the queue
directory's owner before it lands. With no configured queue the alert goes to
the journal only.

Operator lever: `NETWD_TS_MODE` = `live` (default) · `observe` (record + alert,
never restart) · `off`. An unrecognised value degrades to `observe`, never to
`live`. Set it with a drop-in:
`sudo systemctl edit genesis-network-watchdog.service` →
`[Service]` / `Environment=NETWD_TS_MODE=observe`.

Scope: this rides the networkd watchdog, so it is installed only where
systemd-networkd manages the network (see the applicability gate below). A
NetworkManager host running Tailscale does not get it.

## How the body schema surfaces it

The infrastructure profile (`INFRASTRUCTURE.md`, `infra_profile` package)
records the posture as facts, so the annotation layer flags an unprotected
install on its own:

| Key | Kind | Role | Healthy | Defect |
|---|---|---|---|---|
| `networkd_manages_default_route` | fact | posture gate | `true` (networkd owns the route) | — (gate) |
| `networkd_default_route_keepconfig` | fact | posture | `true` | `false` |
| `network_watchdog_enabled` | fact | posture | `true` | `false` |
| `networkd_keep_configuration` | fact | annotation (any link) | `true` | `false` |
| `network_watchdog_installed` | fact | annotation (file present) | `true` | `false` |
| `watchdog` (heal telemetry) | metric | — | rare/zero heals | frequent heals |

### The posture alert (active signal)

The annotation layer is passive prose no one reads. The awareness posture check
(`awareness/loop.py::_check_infra_protection_posture`, the silent-skip closure)
turns the posture facts into an *active* one-shot `high` `infrastructure_alert`
(dashboard + morning report) when a protection is missing, and auto-resolves it
when restored — the same signal the memory plane raises for swap/oomd.

**Two fact granularities, on purpose.** The annotation layer reads the broad
`networkd_keep_configuration` (any link protected) and `network_watchdog_installed`
(timer file present). The posture check instead reads *effective* variants that
measure what actually protects THIS box:

- `networkd_default_route_keepconfig` — KeepConfiguration on the **default-route
  link specifically** (its own `.network.d` drop-in, located via networkctl's
  `NetworkFile`). A protected but unrelated link on a multi-interface box can no
  longer mask a bare default route.
- `network_watchdog_enabled` — the timer is `systemctl is-enabled`, not merely
  installed. The installer deliberately ignores `enable`/`start` failures, so a
  file-present-but-disabled timer would otherwise read healthy while nothing heals.

**The applicability gate.** Both rules are gated on
`networkd_manages_default_route`, the fact that makes this safe on a public repo:
the running systemd-networkd daemon (queried live via `networkctl --json`) must
report the **default-route** interface as `AdministrativeState == "configured"`.
On a NetworkManager box the daemon is not running or reports the link
`unmanaged` — the rules stay silent. A false alert would require networkd to
claim it configures a link it doesn't manage, a contradiction; every doubt path
suppresses (a false-negative is re-checked next collection, never a false alarm
on someone else's install).

> It is queried live rather than by reading `/run/systemd/netif/state`, which
> would be wrong: the unit ships `RuntimeDirectoryPreserve=yes`, so that runtime
> state **survives a networkd stop** and its presence would not prove networkd is
> the active manager.

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
