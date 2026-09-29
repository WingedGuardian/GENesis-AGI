# Network Resilience — KeepConfiguration, a self-healing networkd watchdog, and a Tailscale tunnel watchdog

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
`genesis-tailscale-watchdog.timer` runs `/usr/local/lib/genesis/tailscale-watchdog.py`
as root every ~2 minutes, under the host's `/usr/bin/python3` (standard
library only). It is its own unit, not part of the networkd watchdog, so it
installs on any systemd host that has a `tailscaled` unit, whatever manages the
network.

The failure it targets: every Tailscale SSH session to the box times out while
nothing else looks wrong. The path to the peer works (discovery pings answer),
but WireGuard handshakes stop completing, because the peer keeps replying
through a relay region this node has left. Restarting `tailscaled` on this node
clears it. A peer counts as stuck only when ALL of these hold:

1. it is `Active` with a handshake older than `NETWD_TS_STALE_SEC` (300s). A
   handshake dated in the future means the wall clock stepped back, and a peer
   that has never completed one counts once tailscaled has been up longer than
   the stale age (after a restart that did not clear the fault, that is the
   only form it takes); both are probed rather than skipped;
2. `tailscale ping --tsmp` (through the tunnel) gets no reply;
3. `tailscale ping --until-direct=false` (discovery; a relayed pong counts)
   replies, so the peer is reachable and only the tunnel is dead; and
4. a second `--tsmp` ping still gets no reply (a discovery ping can revive a
   cold path).

The heal is `systemctl try-restart tailscaled`, which drops every Tailscale SSH
session on the box, so it runs at most once per `NETWD_TS_RATE_LIMIT_SEC` (an
hour), measured on the monotonic clock from the later of tailscaled's own start
and the watchdog's last event. What the restart did is read back from systemd,
never from the command's exit code:

| systemd afterwards | Recorded as |
|---|---|
| InvocationID unreadable before the restart | nothing is restarted: an outcome it could not judge is not worth the dropped sessions |
| new InvocationID, unit active, the stuck peer answers through the tunnel within `NETWD_TS_VERIFY_SEC` (60s) | `healed` |
| new InvocationID, unit active, the peer still gets no reply | `restart-no-effect` |
| new InvocationID, unit not active | `restart-failed` |
| same InvocationID, try-restart failed | `not-restarted` |
| same InvocationID, try-restart exited 0 (tailscaled was stopped) | nothing; no hour spent |
| a restart job still queued after the poll | `pending` |
| InvocationID unreadable | `unverified` |

Every run rewrites `/run/genesis-tailscale-watchdog.json` (0644): the run's
state and the last 50 events. A peer is named by its IPv4 address only; its
hostname, which another tailnet member chooses, is never read. At detection the
raw status is also kept as a root-only snapshot,
`/run/genesis-tailscale-watchdog-status.json` (0600).

The owner hears about it through the Genesis runtime: the awareness tick
(`genesis.resilience.tailscale_watchdog_events`) turns each event into an
`infrastructure_alert` observation.

- `healed` is `high` (dashboard and morning report).
- Every other outcome, and `observed` in observe mode, is `critical`, and the
  critical-observations job pages it.
- One stuck tunnel pages once. An event is identified by the boot, the peer and
  that peer's handshake time, which does not move while the tunnel stays stuck,
  and a resolved alert never pages the same incident again.
- When a later run finds tailscaled active and every tunnel answering, open
  critical alerts from the watchdog resolve themselves.
- If the timer is enabled but the file has not been rewritten for 10 minutes, a
  `high` alert says the watchdog has gone silent; if it reports but has checked
  no tunnel for three runs in a row (tailscaled down, the CLI failing, a status
  it cannot parse), a `high` alert says it is blind. Its own failures reach no
  one otherwise: the owning user cannot read the system journal.
- The infra profile records `tailscaled_loaded` and the timer's unit-file state,
  and the protection-posture check flags `tailscale_watchdog_absent` where
  tailscaled is installed and the timer is neither enabled nor masked.
- An `observed` event spends the hour like a restart does, so switching from
  observe to live does not restart for up to an hour after the last
  observation.

Levers, in a drop-in on `genesis-tailscale-watchdog.service`:
`NETWD_TS_MODE=live|observe|off` (an unknown value means observe), and the
`NETWD_TS_*` bounds, each an integer checked against its own range and replaced
by its default, with a journal line, when out of range. To turn it off
durably, `sudo systemctl mask genesis-tailscale-watchdog.timer`: the installer
respects a mask, and otherwise re-enables a disabled timer, as it does for the
networkd watchdog. `scripts/uninstall.sh` removes both root timers.

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
