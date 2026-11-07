- **The FalkorDB default-on clock no longer restarts because another session
  ran tests or a one-off job, or edited unrelated code in the main checkout.**
  The hourly census still lists such processes, but only a process that serves
  graph reads, or an uncommitted edit on the graph-read path, resets the
  14-day clock. Stored census rows are re-judged, so the clock picks up the
  clean days it had already earned.
