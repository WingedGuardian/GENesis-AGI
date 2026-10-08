- Added an opt-in guardian swap ceiling (`swap_ceiling_pct` in a deployed
  install's guardian config): caps this container's share of host swap as a
  percentage of the host's total swap pool, so the cap tracks the pool if it
  is resized. Install-local; the public default remains uncapped.
