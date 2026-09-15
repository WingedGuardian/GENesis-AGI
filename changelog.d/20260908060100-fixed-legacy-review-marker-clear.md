- **Clearing every review marker now also clears the legacy pre-upgrade one**, which
  it could previously skip while reporting success. That legacy file authorizes a
  commit entirely on its own — it needs no per-worktree marker directory to exist, or
  to be readable — but it was removed at the very end of the routine, below a check
  that returns early in exactly those two states. An install carrying only the legacy
  marker, which is what an upgrade leaves behind, therefore got "cleared 0, no
  failures" while a time-valid marker sat intact and could authorize an unreviewed
  commit for its lifetime. It is now cleared before anything can return early, so the
  ordering itself prevents this rather than a second check catching it; a marker
  directory that cannot be read is still reported as the separate failure it is.
