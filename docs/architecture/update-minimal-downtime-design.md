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
