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
| tailscaled's identity (InvocationID, start time, state) unreadable at the scan's start or immediately before the restart | nothing is restarted: a rate limit or an outcome it could not judge is not worth the dropped sessions |
| tailscaled restarted or stopped since the scan began, seen after the scan or immediately before `try-restart` (an operator, an upgrade), or a `try-restart` that found it stopped | `daemon-changed`: every verdict is void and nothing is restarted |
| new InvocationID, unit active, every stuck peer answers through the tunnel within `NETWD_TS_VERIFY_SEC` (60s; it bounds the whole check, and no peer starts a ping after it) | `healed` |
| new InvocationID, unit active, a stuck peer still gets no reply | `restart-no-effect` |
| new InvocationID, unit active, but no check of a stuck peer could be judged (the CLI gave no answer) | `restart-unconfirmed` |
| new InvocationID, unit not active | `restart-failed` |
| same InvocationID, try-restart failed | `not-restarted` |
| same InvocationID, try-restart exited 0 (tailscaled was stopped) | `daemon-changed`; no hour spent |
| a restart job still queued after the poll | `pending` |
| InvocationID unreadable | `unverified` |

Every run rewrites `/run/genesis-tailscale-watchdog.json` (0644). It holds
only what THAT run observed, plus one per-boot count, so there is no condition
to lose or to go stale:

- `evidence`: a verdict per peer the run could judge. `ok` means a handshake
  within the stale age, or the tunnel answered. `stuck` means all four probes,
  in this run. `offline` means the discovery ping failed too: the peer is gone,
  which is not a stuck tunnel. A peer it could not judge is absent from the
  list, meaning unknown: unprobed, idle without a fresh handshake, a daemon too
  fresh to judge a zero handshake, or a ping that hung or failed oddly. The CLI
  exits 1 for many failures (it cannot reach the local tailscaled, no peer has
  that address), so only exit 1 whose last line is `no reply` counts as a
  missing reply: that is the CLI's only no-reply path (tailscale v1.102.4,
  `cmd/tailscale/cli/ping.go`). A run whose backend is not `Running` judges
  nobody.
- The evidence is capped at 1000 peers, keeping `stuck`, then `offline`, then
  `ok` verdicts, so a cap never drops the one that pages.
- `present`: every well-formed peer's IPv4, and whether that list is complete.
- `events`: at most 50 restart outcomes (the table above).
- `unhelped_restarts`: per peer, how many restarts this boot dropped every SSH
  session without a verified heal, kept apart from `events` so trimming that
  list never resets it.

Only peers confirmed stuck IN THE SAME RUN are ever restarted, so a peer that
went offline, or one a later run could not probe, never triggers a restart.
Every restart drops every SSH session, so a peer gets at most three per boot
that did not end in a verified heal, whatever the outcome (`restart-no-effect`,
`restart-unconfirmed`, `unverified`, `pending`, `restart-failed`; a
`not-restarted` restarted nothing and does not count). After that it is not
restarted for again until it is seen working, which resets the count. Its alert
stays open meanwhile.

A peer is named by its IPv4 address only; its hostname, which another tailnet
member chooses, is never read. When a peer is confirmed stuck, the raw status is
also kept as a root-only snapshot, `/run/genesis-tailscale-watchdog-status.json`
(0600).

The owner hears about it through the Genesis runtime. The awareness tick
(`genesis.resilience.tailscale_watchdog_events`) holds the condition as one
`infrastructure_alert` observation per stuck peer:

- `stuck` evidence raises the peer's `critical` alert, which the
  critical-observations job pages once.
- `ok` or `offline` evidence resolves it, and so does the peer's absence from a
  complete `present` list (it left the tailnet). Nothing else does: unknown
  changes nothing, and evidence is used only while the file is fresh.
- A tunnel stuck again within an hour of its alert being resolved (a flap, or
  someone resolving it while it was still true) reopens the same alert without
  paging again.
- Each run that still sees the tunnel stuck moves the alert's expiry a day
  ahead, so it ends on the store's expiry sweep a day after the tunnel was last
  seen stuck (a peer nobody connects to again gives no evidence either way). A
  tunnel seen stuck after that is a new alert and pages.
- Turning the watchdog off withdraws every alert it raised: `off` mode, or,
  once its file has gone stale, a timer that is masked, disabled or not
  installed, or a masked service. The resolution note says nothing is watching any more, which is
  not a recovery. An unreadable timer state withdraws nothing, and a disabled
  timer that is still running is still watching.
- A restart outcome is one alert each. `restart-failed`, `unverified` and
  `pending` are `critical`, and resolve once a later run finds tailscaled
  running with a readable status. `healed` is `high`: the SSH drop was felt
  and the tunnel is back (when one run both finds and heals a tunnel, no
  stuck-peer alert is raised at all). `restart-no-effect`,
  `restart-unconfirmed` and `not-restarted` are `high` because the stuck-peer
  alert, which stays open, carries the page.
- The Genesis side reads the latest run only (every ~5 minutes against runs
  every ~2), so a stuck spell shorter than that can go unreported; restart
  outcomes are kept and never missed.
- If the timer is enabled but the file has not been rewritten for 10 minutes,
  and the oneshot is not mid-run, a `high` alert says the watchdog has gone
  silent. If it reports but judged no tunnel for three runs in a row
  (tailscaled crashed, logged out, the CLI failing, a status it cannot parse,
  or probe limits set to zero), a `high` alert says it is blind. tailscaled
  turned off on purpose (a disabled or masked unit, or `tailscale down`) is
  nothing to watch, not blindness. Both are raised again by a new
  episode after one resolves. The watchdog's own failures reach no one
  otherwise: the owning user cannot read the system journal.
- The infra profile records `tailscaled_loaded` and the timer's unit-file state,
  and the protection-posture check flags `tailscale_watchdog_absent` where
  tailscaled is installed and the timer is neither enabled nor masked
  (a runtime mask counts).
- In observe mode a stuck peer is alerted the same way and never restarted.

Levers, in a drop-in on `genesis-tailscale-watchdog.service`:
`NETWD_TS_MODE=live|observe|off` (an unknown value means observe), and the
`NETWD_TS_*` bounds, each an integer checked against its own range and replaced
by its default, with a journal line, when out of range. **To turn it off
durably, set `NETWD_TS_MODE=off` in that drop-in**: the installer never
touches drop-ins, and off mode withdraws every alert the watchdog raised.
`systemctl disable` does not last, because the installer re-enables a disabled
timer (as it does for the networkd watchdog). No mask works on these units
as installed: `systemctl mask` refuses while the unit file is in
`/etc/systemd/system`, and a `mask --runtime` symlink in `/run/systemd/system`
is shadowed by that file, which comes earlier on systemd's search path
(`systemd-analyze unit-paths`). The installer, the posture rule and the alerts
still respect a mask where one exists (a unit masked before it was ever
installed). The installer never
writes through a symlink: a masked unit is a symlink to `/dev/null`, and
writing and chmodding through it would change `/dev/null` itself.
`scripts/uninstall.sh` removes both root timers, then checks: a unit file or
script it could not remove (it needs sudo), or a timer still active, is named
in a warning rather than reported as removed.

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
