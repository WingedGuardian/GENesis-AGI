- **The session table now tells the truth about which sessions exist and
  which are alive.** Interactive terminal sessions had no creation event —
  a two-hourly poll eventually adopted them, recorded as already finished —
  so a live session read as completed, a young one didn't exist at all, and
  a session that died abruptly stayed "active" forever (no code path ever
  wrote a completion timestamp; the dashboard's failed-in-24h counter also
  sat at zero because its queries referenced columns that don't exist).
  Now: a session-start hook registers an honest active row (with the
  process id) the moment a session begins; adoption records live sessions
  as live and stamps dead ones with their best-known end time; every
  status writer stamps the matching timestamp through one disciplined
  function; each prompt advances the activity clock and un-does a stale
  "completed" verdict; and the liveness reaper gains real evidence — a
  fresh heartbeat always proves alive, a provably-dead process id
  checkpoints the row in ~30 minutes instead of 24 hours (lever:
  `close_dead` / `dead_process_minutes` in the reaper config). The
  dashboard stat queries use real columns, counting historical rows via
  their last activity so no backfill is needed. A fourth, unclosable
  session-row writer in the budget tracker was removed outright.
