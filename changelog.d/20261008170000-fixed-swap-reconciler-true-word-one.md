- Fixed the guardian swap reconciler treating `limits.memory.swap: 1` as a
  one-byte swap ceiling. `1` is an Incus boolean (true), so a live
  `memory.swap.max` of 0 under it is ordinary swap-off and is now healed
  instead of reported and left off.
