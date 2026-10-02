- **An OOM-kill alert is no longer lost when it cannot be queued.** If the
  disk guardian could not write the alert (a full disk is the likely cause),
  the kill was recorded but no page was ever sent. The alert is now kept and
  retried on every poll until it is queued, and a later kill's alert names
  both. Separately, the guardian's "can I see other processes?" check no
  longer counts its own child processes, which matters if it ever runs in a
  private process namespace.
