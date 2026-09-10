- **The related-memory graph no longer goes quietly stale after the database
  recovers from an error.** Genesis keeps an in-process copy of the memory graph
  and rebuilds it when the database it was built from changes. The check for
  "changed" compared the connection object it had been handed — but that object
  is a wrapper, and when the database recovers from a run of errors it replaces
  the connection *inside* the wrapper while the wrapper itself stays the same.
  The check therefore could not notice, and the freshness signal it fell back on
  is per-connection, so it was being compared against a counter belonging to the
  connection that had just been closed.

  The effect was a graph that could keep serving its old contents indefinitely,
  and it was worst at exactly the wrong moment: right after the database had
  trouble, which is when the copy is most likely to be out of date. Genesis now
  tracks the connection underneath the wrapper, so a recovery is seen as what it
  is and the graph is rebuilt once on the next read.
