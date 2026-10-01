# update.sh — Minimal-Downtime Reorder (scoped)

**Status:** design (awaiting sign-off) · **Scope:** `scripts/update.sh` only ·
**Author:** deploy-hardening program (R3) · 2026-07-18

## Why (and why scoped)

`update.sh` unconditionally stops `genesis-server` (line 561) but does not
`git fetch` until line 797 — so the network fetch happens *inside* the downtime
window, and a slow/hung fetch prolongs the outage. Two CLAUDE.md regenerations
(1099–1129) also run while the server is down even though they need neither the
server nor the network.

The original R3 plan also proposed a pre-merge no-op early-exit (skip the stop
entirely when there's nothing to pull). **Dropped**, because verification showed
**nothing runs `update.sh` on a timer or cron** — it runs only when the operator
or the dashboard triggers it, which is almost always a real delta. Optimizing the
never-hit no-op path is not worth new surface area in the deploy script's
rollback region. This scoped version keeps only the wins that apply to *every*
real update.

## What changes (two moves, ~15 lines relocated, no logic rewritten)

### Move 1 — fetch before the stop

Relocate the `git fetch` (currently 797) to **before** the stop block (561),
gated by the existing `POST_MERGE` guard:

```bash
# (new, immediately before the "Pre-update DB snapshot" block ~547)
if [[ "$POST_MERGE" == "false" ]]; then
    echo "--- Fetching latest ---"
    if ! git -C "$GENESIS_ROOT" fetch "$UPDATE_REMOTE" main; then
        echo "  Fetch failed (network?) — server NOT stopped, nothing changed."
        exit 1
    fi
fi
```

The old fetch line at 797 is removed (its `--- Fetching latest ---` echo moves
with it). The merge (836) still consumes `$UPDATE_REMOTE/main` — now already
fetched.

**Why an explicit `if ! … exit 1` and NOT the ERR trap:** the ERR trap arms at
790, *after* the stop. Before the stop there is deliberately no trap — a failure
there must exit cleanly leaving the repo/services untouched (the failed-stop
abort at 571–576 uses exactly this idiom). Routing a pre-stop fetch failure
through the trap would be **actively harmful**: `_do_rollback` (695)
*unconditionally* stops the server and then restarts only `WERE_RUNNING`, which
is empty before the stop — so a transient network blip would **leave the server
down**. The explicit guard avoids the trap entirely and keeps the server up on a
fetch failure. `POST_MERGE=true` (CC conflict-resolution re-entry) skips the
fetch — its code is already merged.

**Win:** the fetch's network time (seconds on a good link, longer on a slow one)
now happens with the server **up**, excluded from the downtime window. Applies to
every real update.

### Move 2 — CLAUDE.md refreshes after the restart

Relocate the two CLAUDE.md regenerations — network-identity (1099–1120) and
container-specs (1122–1129) — from before the restart to **after** the
health-verify block (after 1207), before `_record_update_history "success"`
(1220) / `_write_state "done"` (1228).

Both are pure-local (read local interfaces / a local yaml / the last-collected
profile; no network, no server import — the container-specs block's own comment
says "safe while the server is down"). Moving them out of the stop→restart window
trims a small, fixed cost from downtime.

**Race guard (must preserve):** the server's own infra_profile collector skips
its CLAUDE.md write while `env.update_in_progress()` is true
(`infra_profile/claude_md.py:80`). During a CLI run that stays true — via
`update_state.json` (phase `health_check`, pid `$$`) — until `_write_state "done"`
(1228). Landing Move 2 **after restart but before 1228** means the restarted
server's collector is still gated, so update.sh's own refresh cannot race it.
Keep the network-identity inline `python` heredoc at **column 0** (guardrail
`test_update_host_sync.py:74`).

## What deliberately does NOT move (irreducible offline work)

`git merge` (836, trap-guarded), bootstrap/pip (1009–1015), and migrations
(1050–1059) stay between the stop and the restart. They dominate the window and
cannot move without blue-green (a single editable install swaps code at merge
time). Honest estimate: for a run that merges real changes the wall-clock saving
is **seconds** (fetch + CLAUDE.md out of the window), not a step-change. The value
is robustness (a hung fetch no longer extends an outage), not a downtime rewrite.

## Invariants preserved (each verified against the current script)

- **tag → trap → first-mutation** order holds. The trap still arms at 790 (after
  the stop, before the merge). The pre-stop fetch is NOT under the trap by design.
- **`_do_rollback` force-stop is why Move 1 uses an explicit guard** — never route
  a pre-stop failure through the trap.
- **`_sync_deploy_targets` stays exactly 2 bare calls** (966 no-op path, 1212
  success) — guardrail `test_update_settings_local_transition.py:137`. Move 2 adds
  no call.
- **`WERE_RUNNING` coherence:** populated at 577/584 (in the stop block); the
  pre-stop fetch adds no reader of it. The restart/rollback consumers
  (721/945/1140/1188) are untouched.
- **`POST_MERGE` gating:** the pre-stop fetch is `POST_MERGE==false`-gated so the
  `--post-merge` CC-conflict re-entry (which must not re-fetch) skips it.
- **Marker race (Move 2):** refresh lands after restart, before `_write_state
  "done"` (1228) — server collector stays gated.
- **Alternate exits unchanged:** up-to-date (940–977) and merge-conflict (838+)
  keep their own restart/exit semantics; the stop still precedes the merge.
- `bash -n` clean; the 3 BEGIN/END marker blocks stay intact and isolated.

## Later additions to the pre-stop window (2026-09-30)

Four steps now share the window this reorder opened, and each keeps its rule: a
failure before the stop exits with nothing stopped and nothing changed, never
through the rollback trap.

- **The deployable-checkout checks run first of all**, before the lock, the
  rollback tag and the backup. They are the shared ones in
  `scripts/lib/deploy_checkout.sh`, lifted from `deploy_code_only.sh`, which calls
  the same functions. A linked worktree, a bare repository, a detached HEAD or a
  branch other than `$DEPLOY_BRANCH` is refused before any state is touched.
- **The fetched head is pinned** from a per-run ref (`refs/genesis/update/<pid>`)
  that the same fetch writes beside the tracking ref, read once and deleted at once.
  The merge takes that commit. Neither `FETCH_HEAD` nor the tracking ref decides the
  pin: other sessions fetch in the same checkout and rewrite both.
- **The checkout must not move under the run.** `ORIGINAL_BRANCH` is the branch
  the checks validated, not a later re-read. `genesis_checkout_unmoved` re-checks
  branch and commit before the rollback tag (the pre-update backup can take
  minutes), and `checkout-unmoved` re-checks them, plus "no new tracked edit",
  before the clears and again just before the merge. `_do_rollback` resets only
  from `UPDATE_OWN_HEAD`, the commit this run's merge produced. At the rollback
  commit there is nothing to undo, so it skips the reset. A clean branch switch
  (the original branch still at this run's state, nothing uncommitted) is reversed
  with a non-forced checkout. Any other move is left alone. Dependencies and
  services come back only on the pre-update code (the original branch at the
  rollback commit, no foreign tracked edit); otherwise the rollback reports itself
  incomplete. Two cases are still open: an uncommitted edit made on the same branch
  after the merge is lost to a post-merge reset (#2679), and the watchdog restarts
  a server the rollback held down within one tick (#2718).
- **Incoming changes that would overwrite a local untracked or ignored file are
  refused** (`genesis_range_collisions`, over the range from the merge base, every
  change but a deletion: an incoming MODIFICATION of a path the local branch
  deleted and keeps an ignored copy of is overwritten in the modify/delete
  conflict too). The
  merge also passes `--no-overwrite-ignore`, but git 2.43 honours that only on a
  fast-forward; a true 3-way merge overwrites the file, and a rollback's
  `reset --hard` then deletes it. So the scan is the protection: before the stop,
  and again as the last step before the merge (`late-collision-scan`), where a hit
  rolls back with HEAD unmoved: the rollback resets nothing, so the file stays.
  Paths git invents in a file/directory conflict (`<path>~HEAD`) are not scanned
  (#2678).
- **Local edits to the ephemeral files are backed up** (`ephemeral-prestop-backup`)
  between the fetch and `_write_state "fetching"` — every dirty one, because a
  rollback's `reset --hard` discards any of them. The clear before the merge
  touches only the files the incoming range changes, plus any with a STAGED edit
  (git keeps an unstaged edit to any other file through the merge, but a true
  3-way merge refuses on any index change), and discards a file's edits only when a backup of
  its current content exists. `_do_rollback` saves any dirty ephemeral file whose
  current edits have no backup (a `--post-merge` run, or an edit made after the
  backup) before its `reset --hard`. After the stop, the merge is also checked
  against the branch the run started on.

## Success records name a server that did not come back (2026-09-30)

A change at the success writers, outside the pre-stop window: every `success` row
(P6 and both no-change writers) builds its degraded value through
`_success_degraded_subsystems`, so each can carry `genesis-server-not-restarted`.
P6 decides it by whether genesis-server was in `WERE_RUNNING` (not by
`_OPERATOR_STOP`, which is true only when `WERE_RUNNING` is entirely empty). A
bridge-only run does not reach P6's success writer at all today: its health gate
checks genesis-server whenever `WERE_RUNNING` is non-empty, and rolls back. The
no-change path, which has no health loop, decides it with `_server_health_ok`:
P6's per-attempt probe, retried while the unit reports itself starting (or while a
direct-started process lives), bounded by elapsed time at 180s. Unit state alone is
not used, because a crash-looping unit reads `activating`. The no-change path writes a row
only to persist a degradation, as before, and with the server down only over
nothing-recorded or a prior `success` (an allowlist): any other latest status
stays the latest. The no-change path and P6 read the last status through one
reader (`_latest_update_status`), which reads the install's own database with any
stdlib interpreter and reports `unreadable` rather than "nothing recorded" when it
cannot tell.

No reader of `update_history` consults `degraded_subsystems` today, so the marker
informs a human reading the history; it does not yet change what the deploy-state
readers treat as deployed.

## New test — phase-order lock

`tests/test_scripts/test_update_phase_order.py` (extraction-style, reads the
script text; no execution):
- `index("git … fetch main")` < `index("Stopping services")` < `index("git …
  merge")`.
- the pre-stop fetch sits inside a `POST_MERGE == "false"` guard.
- both CLAUDE.md sentinel-writer calls (`write_sentinel_block … network-identity`
  and `--claude-md-block`) appear **after** the `Restarting services` marker and
  **before** `_write_state "done"`.
- rollback-tag creation < `trap _on_err ERR` < `Stopping services`.

Plus extend any existing `test_update_*` that asserts on the moved regions.

## Verification / E2E (post-merge, on this install)

1. **Deploy run** ships the reorder (old logic governs the shipping run — bash
   already loaded the old bytes; the reorder governs the *next* run).
2. **Real-delta run** (next actual update): confirm the server-down window
   (`systemctl --user show -p ActiveEnterTimestamp genesis-server`) no longer
   spans the fetch (journal shows fetch before the stop), health gate passes,
   rollback tag cleaned, CLAUDE.md blocks refreshed post-restart.
3. **Fetch-failure injection (scratch clone, NOT prod):** point the remote at an
   unreachable URL; confirm the server is **never stopped** and the script exits 1
   without rollback.
4. **Bootstrap-failure injection (scratch clone):** confirm `_do_rollback` still
   restores + restarts (unchanged path).

## Confidence: 88%

Higher than the full-R3 85% because the riskiest edit (no-op early-exit above the
stop) is dropped. Residual 12%: the exact placement of Move 2 vs the marker clear
(mitigated — traced to line 1228) and that the pre-stop fetch must never reach the
trap (mitigated — explicit guard + order-lock test + fetch-failure E2E).
**DISPROVEN if:** a fetch failure leaves the server stopped, OR the restarted
server's collector races update.sh's CLAUDE.md write, OR `_sync_deploy_targets`
call count changes.
