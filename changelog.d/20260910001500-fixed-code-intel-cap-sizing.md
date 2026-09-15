- **The code indexer no longer gets killed for being the right size.** Indexing
  this repository needs about 2.8 GB, but the job was capped at 2 GB — so it was
  killed partway through, every time, and the code intelligence it produces went
  stale without anything obviously failing. The cap now comes from that
  measurement, and it is bounded so it can never approach the limit of whatever
  it is actually running inside: not just the machine's own ceiling, but any
  tighter limit set anywhere above the job, since the tightest one is what really
  applies. On a smaller install the job gets less rather than being handed a cap
  equal to all the memory there is, which would protect nothing. If the limit is
  unknown or too small to leave the safety reserve, indexing is refused before
  launch rather than risking the rest of the machine.
