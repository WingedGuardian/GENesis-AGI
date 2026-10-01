- **Guardian: pool relief acts early, from measured growth, and can use VG
  space LVM's autoextend could not.** Relief now keeps a 7-day pool history
  and also acts before the 3% / 10% reserve, when data or metadata would fill
  within 48 hours at the measured rate. Early relief frees pre-recovery and
  superseded snapshots, and the rollback snapshot only once it is older than
  48 hours and (on LVM) measured holding space; a younger one is still only
  deleted at the reserve. On an LVM thin pool with the `genesis-thinpool`
  profile, when data reaches 80% and the volume group has free space but less
  than one 20% autoextend step (so LVM would not extend at all), relief grows
  the pool by that space, keeping some unallocated for metadata. New
  `storage_pool` keys: `early_horizon_hours`, `lifeline_max_age_hours`,
  `history_sample_interval_s`, `history_max_samples`, `extend_keep_free_mib`.
