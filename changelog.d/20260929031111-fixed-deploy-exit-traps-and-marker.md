- **`update.sh` no longer deletes the wrong file on exit.** Its exit trap named its
  temp copy through a path bash evaluates when the trap fires, so an exit from
  inside a sourced library function (an unbound variable is enough) deleted the
  library and leaked the copy. And a server started by its `nohup` fallback
  inherited the "running from the copy" flag, so a dashboard update launched from
  that server ran in place and its trap deleted `scripts/update.sh` itself. The
  path is now bound once at startup, and "we are the copy" is proven by the
  copy's own path.
- **`update.sh`'s Guardian lease renewer no longer holds the update lock after it
  exits.** The renewer's `sleep` inherited the lock and outlived the deploy, for up
  to 15 minutes after a clean exit and about an hour after a kill. It is now
  started without the lock, and it stops once the deploy that started it is gone —
  judged by the process's identity (pid and start time), so a killed deploy that
  lingers as an unreaped zombie, or a new process that reuses its pid, does not
  keep the Guardian paused. The pause, resume and renewer moved to
  `scripts/lib/guardian_pause.sh`; the deploy marker and the list of tracked files
  a deploy may find dirty moved to `scripts/lib/deploy_marker.sh`, shared with
  `restore.sh`.
- **A zombie no longer holds the deploy marker.** A deploy that was killed but not
  yet reaped left `~/.genesis/update_in_progress.pid` naming a zombie, and every
  liveness check read that as a deploy still running: new deploys refused, and the
  watchdog kept from restarting a down server. The marker's holder now counts only
  if it is running and not a zombie, in the deploy scripts and the watchdog's
  reader alike. The holder check compares no clocks, so a wall-clock step cannot
  make a live holder read as stale.
- **`restore.sh` refuses when it cannot write the deploy marker.** The write was
  unchecked, so an unwritable marker read as held: the restore stopped the server
  and rebuilt the database without the marker the watchdog defers on, which let
  the watchdog restart the server mid-rebuild. It now refuses before stopping
  anything, and says why.
