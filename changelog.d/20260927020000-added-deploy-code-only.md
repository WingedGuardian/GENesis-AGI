- **A locked code-only deploy: `scripts/deploy_code_only.sh`.** Runtime code is an
  editable install, so most changes deploy by pulling main and restarting the
  server. Done by hand, that took no lock and could land in the middle of
  another session's validation against the live server, or of `update.sh`. The
  script takes the same lock `update.sh` takes, exclusively, and queues for it.
  A validation run holds the lock shared
  (`flock -s -w 7200 ~/.genesis/locks/update.lock <cmd>`), so a deploy waits for
  it to finish; `update.sh` keeps refusing rather than queuing. The hold does not
  stop restarters that take no lock (the watchdog, the dashboard's service routes,
  Guardian recovery), so a validation also compares the server's MainPID and the
  checkout's HEAD at its start and end.
  - **It refuses before anything changes:** a linked worktree, an unfinished
    `update.sh` run, a branch other than main, a dirty tracked tree, a unit that
    runs a different venv, a live foreign deploy marker, a diverged tree, and a
    venv that no longer describes the tree being deployed. That last check reads
    the installed project's own metadata: it must be an editable install from
    this checkout, and its requirements (base and every optional group, compared
    parsed) and `requires-python` must equal the incoming `pyproject.toml`'s. Any
    difference routes to `update.sh`, whose reinstall clears it. A state
    file left by an `update.sh` run that finished (phase `done`) does not block.
  - **Across the restart** it holds the deploy marker (the watchdog defers) and
    pauses the host Guardian (no false "Genesis down" alert), then waits for
    health with the same window `update.sh` computes.
  - **Healthy means the RESTARTED unit is serving:** the unit is active with a
    new pid, and every socket listening on the health port is one of that pid's
    own (read from `/proc`, no external tool; when ownership cannot be read the
    answer is no). A server running outside systemd, or any other process holding
    the port, does not count. The subsystems are
    then compared with the pre-restart manifest (the same check `update.sh` runs,
    now shared in `scripts/lib/manifest_delta.py`); a regression is reported and
    alerted, and the deploy stands.
  - **On a failed health check it alerts and holds.** The tree is not reverted.
  - `--no-restart` is a locked pull only, for Claude Code hooks (they run from
    the tree, so the pull is the deploy) and docs. Git hooks are copies installed
    by `update.sh`; a range that changes them is named in the activation note
    below. It refuses a range
    touching anything outside `.claude/`, `docs/`, `tests/`, `changelog.d/`,
    `.github/`, `scripts/hooks/` and top-level Markdown — `config/` is read at
    server start. `--no-pull` restarts the tree as it stands (a locked restart).
  - It says what a code-only deploy does not apply: activation paths (units,
    bootstrap, git hooks, dependencies) and code the host Guardian runs, both
    of which need `update.sh`.
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
  keep the Guardian paused.
  The pause, resume and renewer moved to `scripts/lib/guardian_pause.sh`, shared
  with the new script; the deploy marker and the list of tracked files a deploy
  may find dirty moved to `scripts/lib/deploy_marker.sh`, shared with
  `restore.sh`.
- **A zombie no longer holds the deploy marker.** A deploy that was killed but
  not yet reaped left `~/.genesis/update_in_progress.pid` naming a zombie, and
  every liveness check read that as a deploy still running: new deploys refused,
  and the watchdog kept from restarting a down server. The marker's holder now
  counts only if it is running and not a zombie, in the deploy scripts and the
  watchdog's reader alike. The holder check compares no clocks, so a wall-clock
  step cannot make a live holder read as stale.
