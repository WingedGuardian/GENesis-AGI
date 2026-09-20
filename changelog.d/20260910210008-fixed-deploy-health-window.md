- **A slow boot is no longer mistaken for a failed deploy.** The post-update
  health gate gave the server a fixed three minutes to answer, but a busy
  machine can take five just to finish starting — and the gate then rolled a
  perfectly good deploy back to the old code. The gate now watches the server
  process itself: while it is alive and starting it keeps waiting, and the
  moment the process dies the wait stops early instead of padding out the
  clock. The window is fifteen minutes — three times the slowest boot actually
  seen — and its ceiling is not a chosen number: it is derived from how long
  the host watchdog has been asked to stand down, because a deploy that waits
  longer than that leaves the watchdog looking at a machine it has been told
  to ignore. Raising one now raises the other, and asking for more than the
  ceiling says so instead of silently truncating. Adjustable via
  `GENESIS_DEPLOY_HEALTH_WINDOW_SECS`, never less than the old three minutes.
  The wait is measured on a clock that cannot be stepped, so an NTP correction
  mid-deploy can no longer cut it short or stretch it, and a moment where the
  service state cannot be read is treated as "ask again", not as a death. The
  rollback message now states how long it waited and what state the service
  was in.
