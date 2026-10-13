- **The cc-tmp watchdog's ORANGE tier no longer kills idle terminal sessions.**
  At 75% of the cc-tmp budget the watchdog used to reap unattached CC tmux
  sessions idle more than two hours. It never fired and could not have helped if
  it had. Measured directly in the current log: the loop was reached on 356
  ORANGE polls and killed nothing; a further ~1,029 polls with no kills are
  reported in the pull request that first raised this, from a window this
  install's rotated log can no longer confirm. The code's own comment already
  gave the reason — sessions are not what fills cc-tmp — and one measured
  episode is the demonstration: an install sat ORANGE for 2h45m on a
  third-party tool's index cache plus live session trees, none of which a
  session kill would have touched. ORANGE now records the stuck state and
  stops; the RED tier (90%) still kills every unattached session and is
  unchanged. One residual, stated rather than hidden: killing a process closes
  its descriptors and so releases any unlinked-but-held blocks it pinned, which
  a directory-usage figure cannot see. That reclaim was never reachable from
  this tier, for a reason the old predicate hid — it judged idleness with
  tmux's session-activity timestamp, which tracks terminal output rather than
  the process, so "idle" meant "not printing", never "not writing". Measured: a
  session writing 8MB/s to disk advanced that timestamp by 0 seconds. The loop's
  best case was freeing space the tier could not measure, and its worst case was
  reaping a session that was working.
