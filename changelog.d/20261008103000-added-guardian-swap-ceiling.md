- Added an opt-in guardian swap ceiling (`swap_ceiling_pct` in a deployed
  install's guardian config): caps this container's share of host swap as a
  percentage of the host's total swap pool, enforced through Incus's own
  native swap-ceiling key or a direct cgroup write when no memory cap is set.
  Install-local; the public default remains uncapped.
