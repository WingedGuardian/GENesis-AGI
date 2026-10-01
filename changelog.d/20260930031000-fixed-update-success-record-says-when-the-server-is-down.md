- **A deploy that left the server down now says so in its "success" record.** Every
  success entry `update.sh` writes now carries `genesis-server-not-restarted` when the
  server was not brought back, including the no-change path. On the no-change path, "brought back" now means the health endpoint
  answers within a bounded wait, not that the unit merely reads as active (a
  crash-looping server does),
  and the leftover update-failure marker is cleared only then. With the server down, a
  no-change run records nothing over a failed, rolled-back or other unresolved update,
  so the next run still recovers it. The status it reads comes from the install's own
  database rather than a fixed home path, an unreadable status is treated as a
  possible failure (recover, and run the full activation) rather than as "nothing
  happened", and a missing venv now prints a warning that the history entry could not
  be written, instead of skipping it silently.
