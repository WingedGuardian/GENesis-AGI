- **A running session can no longer be told its work was interrupted.** The
  liveness reaper checked "is this session alive?" once at the start of a pass
  and then worked through rows one at a time — so a session that came back to
  life partway through that pass could still be checkpointed, and its user got
  a message saying nothing was running on a request that was in fact still
  running. The aliveness check now happens again at the moment of the write,
  inside the same database statement, so there is no window between the two.
- **A session routed to a non-default model records that model, not "Claude".**
  Sessions launched against an alternate model were registered under the model
  Claude Code reports for itself, so the dashboard showed the wrong one and
  disagreed with the session's own heartbeat record.
- **The "last 24 hours" session counts really are the last 24 hours.** Two more
  status queries compared timestamps as text, which let anything sharing the
  cutoff's calendar date through regardless of its time — measured on a live
  install, 45 rows counted where 33 belonged, some of them over a day old.
  The overcount grows through the day and resets at midnight.
- **One malformed session row no longer silences the whole sweep.** An
  unexpected error while checking whether a recorded process was still alive
  could abort an entire reaper pass, leaving every other finished session
  marked active until the next run.
