- **Uninstall left stale schedule state behind for three timers.** A timer
  marked `Persistent=true` records when it last ran, in a stamp file that
  removing the unit does *not* delete. `systemd.timer(5)` says to clear that
  state *before* uninstalling such a unit, and `uninstall.sh` does — from a list
  of timer names maintained by hand, which had fallen behind the timers that
  actually exist.

  `genesis-code-intel.timer` and `genesis-backup.timer` were both missing from
  it, and from the disable list as well. Removing the unit files by wildcard is
  not a substitute for either step: it leaves the stamp in place, and it leaves
  dangling enabled links behind. The practical effect is that reinstalling could
  inherit a stale "last run" and immediately replay a run it should have
  skipped.

  All three timers — including the new projection timer — are now disabled and
  have their state cleared, and a test derives the list of `Persistent=true`
  timers from the templates themselves and checks each one is covered. The
  previous test enforced this for a single named timer, so it could not see the
  next two.
