- **A locked code-only deploy: `scripts/deploy_code_only.sh`.** Runtime code is an
  editable install, so most changes deploy by pulling main and restarting the
  server. Done by hand, that took no lock and could land in the middle of
  another session's validation against the live server, or of `update.sh`. The
  script takes the same lock `update.sh` takes, exclusively, and queues for it.
  A validation run holds the lock shared
  (`flock -s -w 7200 "${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock" <cmd>`), so a deploy waits for
  it to finish; `update.sh` keeps refusing rather than queuing.
  - **Four modes.** `deploy` (the default) pulls main and restarts. `pull` pulls
    and syncs the git hook copies without restarting, and accepts any range: it
    names every change to `src/`, `config/` or `pyproject.toml` that the running
    server has not loaded, and the next step. `restart` restarts the tree as it
    stands. `status` is read-only and takes no lock.
  - **What the server booted from.** Every mode reports the commit the running
    server started from against the tree, read from HEAD's reflog at the unit's
    start time. It is "unknown" when the reflog does not reach back that far,
    has a gap left by pruned entries, does not end at HEAD, shows the clock
    stepping back across the boot, has a move in the boot's own second, or when
    the boot is older than git's expiry for unreachable reflog entries (which
    can remove a detour as a pair without leaving a gap). The reflog cannot show
    a manual `git reflog expire --rewrite`, a move whose committer time was
    backdated, or one made after the clock stepped back behind the boot; the
    server recording its own commit at boot is the complete answer.
  - **`deploy` restarts only for what the server loads:** when the files under
    `src/`, `config/` and `pyproject.toml` are the ones the server booted from,
    after the merge and at every commit HEAD has held since the boot (a docs or
    hooks range, or nothing to merge), it neither stops nor restarts the server.
    The history matters because the server imports lazily: after a `pull` of
    code, a module it loaded stays in memory even once a later commit restores
    the files. `restart` forces one.
  - **It refuses before anything changes:** a linked worktree, an unfinished
    `update.sh` run, a branch other than main, a dirty tracked tree, a unit that
    runs a different venv or from a different directory, a live foreign deploy
    marker (or one that cannot be written), a diverged tree, and a venv that
    does not match the
    `pyproject.toml` being deployed. That check reads the installed project's own
    metadata: an editable install from this checkout, with requirements (base and
    every optional group, compared parsed) and `requires-python` equal to the
    incoming file's, every base requirement actually present at a version it
    accepts, and the installed entry points (console commands, plugins) equal to
    the file's, since a new command gets its shim only from a reinstall. The refusal names the command that fixes it: `update.sh` when there
    is a range to merge, `update.sh --post-merge` at the tip (plain `update.sh`
    stops at "Already up to date" without reinstalling, unless update.sh-only
    paths changed since its last recorded run), and neither when the
    venv's python is older than the incoming `requires-python`.
  - **`deploy` stops the server before the fast-forward**, as `update.sh` does,
    so no request runs against a mix of old and new modules. A merge git refuses
    starts it again on the unchanged tree, and any failure while it is stopped
    starts it before exiting, and so does a restart whose start half fails.
  - **Just before any restart it checks again** that the checkout is the exact
    commit it checked, with no tracked change and no untracked file under
    `src/`, `config/` or `pyproject.toml`, and that no server is running outside
    the unit (such as `update.sh`'s fallback, which a restart would not replace).
    `deploy` and `restart` also refuse those last two before anything changes.
    If a late check fails after `deploy` stopped the server, refusing would leave
    it down, so it is restarted on the tree as it stands with its health check,
    and the run ends in a critical alert naming what changed. The git hook
    copies are synced after the restart, not while the server is down.
  - **A range that adds a file already present here, untracked, is refused.** A
    fast-forward overwrites an ignored file without asking, so a local secrets or
    settings file would be lost. A tracked file or directory the range replaces
    is git's own and does not count. The merge itself runs with
    `--no-overwrite-ignore`, so an ignored file that appears after that check
    (written by the server, or another session) is refused by git at the merge,
    and a stopped server goes back up on the unchanged tree. The regenerable files
    reset for the merge (`AGENTS.md`, the procedure trigger cache, which update.sh
    discards the same way) are named as they are reset, in a refusal's message and
    in the exit alert, so a refused merge never reads as "nothing changed" in
    silence.
  - **Across a restart** it holds the deploy marker (the watchdog defers) and
    pauses the host Guardian (no false "Genesis down" alert), then waits for
    health with the same window `update.sh` computes. The health request goes to
    the loopback address with no proxy and no `.curlrc`, and counts only if the
    same unit pid owns the port before and after it.
  - It queues for the lock for up to two hours by default, as long as a
    validation's documented hold. Healthy means the
    RESTARTED unit is serving: the unit is active in a new systemd activation (its
    invocation id, since the kernel can hand the new process the old pid), and every
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
  - For a validation against the live server, the script judges the bracket:
    take the `bracket:` token `status` prints at the start, and run
    `status --verify <token>` at the end (exit 0 is valid). A token exists only
    when the boot commit is known, HEAD's runtime files are the ones the server
    booted from and were at every commit HEAD held since the boot, and nothing
    under them is edited outside git. It covers a
    restart (boot commit, MainPID, systemd invocation id, which a reused pid
    cannot fake) and an edit to an ignored runtime override such as a
    `config/*.local.yaml`, fingerprinted without the files the server rewrites on
    its own, and the user overlays in `~/.genesis/config` that the loaders prefer.
    HEAD may move over docs or hooks without invalidating the run. It is a
    tripwire, not a certificate: it cannot see, and reads valid through, a change
    to the venv's installed packages, the other files the server reads from
    `~/.genesis/config` (only the `*.local.yaml` overlays are fingerprinted), an
    edit undone without moving HEAD (a stash and its pop included), and a
    rewritten or backdated reflog. Proving what the server runs needs the server
    to report its own identity.
