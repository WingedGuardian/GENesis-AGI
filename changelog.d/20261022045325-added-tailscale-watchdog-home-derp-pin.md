- **The Tailscale watchdog can keep this node's home DERP region pinned.** Set
  `NETWD_TS_PREFER_DERP=<region id>` in a drop-in on
  `genesis-tailscale-watchdog.service` (off by default) and each run keeps the
  node homed on that region with `tailscale debug force-prefer-derp`,
  re-applying it whenever the relay drifts off the region or tailscaled has
  restarted (including the watchdog's own restart heal), since a restart clears
  the pin. This avoids a one-way tunnel that can follow a home-region change
  when a peer keeps sending to the old region. A lapse (the home left the
  pinned region while tailscaled kept running: either the region was
  unreachable at a netcheck, or the pin was cleared) raises an owner alert
  naming the result of the re-apply, as does a pin that cannot be applied or
  judged; observe mode reports and changes nothing.
