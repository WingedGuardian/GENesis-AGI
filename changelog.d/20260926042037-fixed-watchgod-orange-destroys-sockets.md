- **The cc-tmp watchdog's ORANGE tier no longer destroys unix sockets when it
  evicts a cache.** The RED tier's cache sweep was routed through the
  socket-sparing reclaim path after a measured 2026-09-05 incident, in which
  sessions were left listening on bound-but-unlinked sockets and inbound
  connections failed. The identical sweep one tier down was left as a plain
  recursive delete, which has no socket predicate — and that tier fires at 75%
  of the budget while the guarded one waits for 90%, so the unprotected copy is
  the one that actually runs. Reproduced: a live socket inside the skills cache
  was destroyed by the earlier tier. It now uses the same object-level reclaim,
  which still removes a cache directory holding no sockets exactly as before.
  Sockets are 0 bytes, so sparing them reclaims precisely as much as deleting
  them did.
- **The watchdog's reclaim loops can no longer follow a directory name out of
  the tree they were pointed at.** All three read the output of `find` one line
  at a time, so a directory whose *name* contained a newline split into two
  records: a head that was skipped, and a tail that was a bare relative path —
  resolved against the daemon's working directory, which is the invoking user's
  home rather than the temp tree. Measured: a cache directory named `tsx-a`,
  newline, `b` produced a second record of just `b`, which matched a real
  directory beside the daemon's cwd. Two failures at once — the real directory
  was never reclaimed, and an unrelated one could be. The loops are now
  NUL-delimited, which is exactly one correct record per match. No known
  program creates such a name; this closes the shape rather than a sighting.
