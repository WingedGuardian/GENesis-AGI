- **An archived worktree's commits can no longer be garbage-collected while the
  archive exists.** The reaper locked the worktree's registration and treated that
  as the anchor for its commits, but a lock only protects the registration. Once
  the archived branch was deleted, the worktree's HEAD reflog was the only
  reference left, and it expires after 30 days by default, after which `git gc`
  collected the commits and `--recover` restored files with nothing under them.
  Each archive now also gets its own `refs/archived/<entry>` ref, which keeps the
  commits reachable regardless of reflog expiry.
