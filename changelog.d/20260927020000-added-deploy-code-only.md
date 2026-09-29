- **A locked code-only deploy: `scripts/deploy_code_only.sh`.** Runtime code is an
  editable install, so most changes deploy by pulling main and restarting the
  server. Done by hand, that took no lock and could land in the middle of
  another session's validation against the live server, or of `update.sh`. The
  script takes the same lock `update.sh` takes, exclusively, and queues for it.
  A validation run holds the lock shared
  (`flock -s -w 7200 ~/.genesis/locks/update.lock <cmd>`), so a deploy waits for
  it to finish; `update.sh` keeps refusing rather than queuing.
  - **Four modes.** `deploy` (the default) pulls main and restarts. `pull` pulls
    and syncs the git hook copies without restarting, and accepts any range: it
    names every change to `src/`, `config/` or `pyproject.toml` that the running
    server has not loaded, and the next step. `restart` restarts the tree as it
    stands. `status` is read-only and takes no lock.
  - **What the server booted from.** Every mode reports the commit the running
    server started from against the tree, read from HEAD's reflog at the unit's
    start time. It is "unknown" when the reflog does not reach back that far,
    has a gap left by pruned entries, does not end at HEAD, has times going
    backwards, has a move in the boot's own second, or when the boot is older
    than git's expiry for unreachable reflog entries (which can remove a detour
    as a pair without leaving a gap). The reflog cannot show a manual
    `git reflog expire --rewrite` or a move whose committer time was backdated;
    the server recording its own commit at boot is the complete answer.
  - **`deploy` with nothing to deploy does not restart:** when nothing merged
    and the server provably booted from HEAD, it says so and stops. `restart`
    forces one.
  - **It refuses before anything changes:** a linked worktree, an unfinished
    `update.sh` run, a branch other than main, a dirty tracked tree, a unit that
    runs a different venv, a live foreign deploy marker (or one that cannot be
    written), a diverged tree, and a venv that does not match the
    `pyproject.toml` being deployed. That check reads the installed project's own
    metadata: an editable install from this checkout, with requirements (base and
    every optional group, compared parsed) and `requires-python` equal to the
    incoming file's, and every base requirement actually present at a version it
    accepts. The refusal names the command that fixes it: `update.sh` when there
    is a range to merge, `update.sh --post-merge` at the tip (plain `update.sh`
    stops at "Already up to date" without reinstalling, unless update.sh-only
    paths changed since its last recorded run), and neither when the
    venv's python is older than the incoming `requires-python`.
  - **Across a restart** it holds the deploy marker (the watchdog defers) and
    pauses the host Guardian (no false "Genesis down" alert), then waits for
    health with the same window `update.sh` computes. Healthy means the
    RESTARTED unit is serving: the unit is active with a new pid, and every
    socket listening on the health port is one of that pid's own (read from
    `/proc`; when ownership cannot be read the answer is no). The subsystems are
    then compared with the pre-restart manifest, as `update.sh` does; a
    regression is reported and alerted, and the deploy stands.
  - **On a failed health check it alerts and holds.** The tree is not reverted.
  - It names what a code-only deploy leaves to `update.sh`: activation paths
    (units, bootstrap, the Claude Code pin, dependencies), code the host Guardian
    runs, and code the tmp watchgod runs from the tree.
  - Launch `deploy` and `restart` detached, as a transient `systemd-run --user`
    unit named with the time, so a second launch queues on the lock (the command
    is in the script's header): a session's background job dies with the session.
  - For a validation against the live server, `status` at the start and the end
    is the bracket: the run is invalid unless the server booted from HEAD at the
    start, and the boot commit, HEAD and MainPID are unchanged at the end.
