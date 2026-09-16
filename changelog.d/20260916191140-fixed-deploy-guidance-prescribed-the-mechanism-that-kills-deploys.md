- **The deploy guidance no longer prescribes the one mechanism that has killed
  deploys, and the advisory no longer goes quiet when you use it.** Both the
  genesis-development skill and the pre-deploy advisory told sessions to run
  `update.sh` in the background — and the advisory returned early, saying nothing,
  whenever a command actually did. Deploys started that way have been killed
  mid-run twice, once leaving the server down through bootstrap. Nothing ever
  contradicted the advice in practice, because the guard only spoke when the advice
  was being ignored.

  Both surfaces now prescribe handing the deploy to systemd as a detached unit, and
  the advisory fires whether the deploy was foregrounded or backgrounded.

  The advice is also narrower than it was, and deliberately so. It covers
  `update.sh` only. `bootstrap.sh` and `host-setup.sh` both need a channel a
  detached service does not have — bootstrap calls `sudo` unconditionally, and
  host-setup is interactive with a recreate prompt whose no-input default stops and
  renames the existing container. `update.sh` is the only one of the three with no
  `sudo` calls at all, and the only one actually exercised this way end to end. The
  advisory no longer mentions the other two rather than offer a recipe that breaks
  or destroys something.

  One thing measured while fixing it, which our own notes had stated the other way
  round: backgrounding has no ten-minute ceiling — a 400-second task completed
  cleanly — so the problem was never a timeout. A background task is bound to the
  session, which is a different failure and needs a different fix.

  The skill also now describes what a mid-run kill actually does, which depends on
  the phase: during the pre-update backup no signal handler is installed at all,
  before the merge an interrupt restarts the services it stopped, and from the merge
  onward it rolls back. That distinction matters for diagnosis, because "no handler
  ran" does not tell you which signal arrived.

  Three things a reader can now rely on that were previously wrong or missing.
  `systemd-run --user --scope` does **not** detach — it isolates the cgroup and
  keeps the caller's session — and neither does `setsid`, which starts a new session
  without stopping the caller waiting on it. `--setenv=PATH` is required, because a
  user unit otherwise runs under a PATH that omits `~/.local/bin` and makes
  bootstrap take different branches. And "no signal handler ran" does **not** mean a
  deploy was SIGKILLed: the handlers are installed after the pre-update backup, so a
  kill during the backup leaves the same evidence either way.

  A stale comment in `update.sh` that described the dashboard's direct update path
  as un-isolated and a pending bug is corrected too — it isolates today, and the
  warning now sits on the fallback branch where it still applies.

  The advisory deliberately does not try to work out whether you have already
  detached; it says so, and over-fires rather than risking silence. Narrowing that
  is tracked separately.
