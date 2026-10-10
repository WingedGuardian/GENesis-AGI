- Added an opt-in guardian swap ceiling (`swap_ceiling_pct` in a deployed
  install's guardian config): caps this container's share of host swap as a
  percentage of the host's total swap pool, enforced through Incus's own
  native swap-ceiling key under a hard memory cap, or a direct cgroup write
  otherwise (no memory cap, or `limits.memory.enforce: soft`). Set
  `swap_ceiling_pct: off` to remove a ceiling; removing the setting leaves
  whatever is set alone. Install-local; the public default remains uncapped.
