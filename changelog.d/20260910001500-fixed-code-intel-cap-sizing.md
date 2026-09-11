- **The code indexer no longer gets killed for being the right size.** Indexing
  this repository needs about 2.8 GB, but the job was capped at 2 GB — so it was
  killed partway through, every time, and the code intelligence it produces went
  stale without anything obviously failing. The cap now comes from that
  measurement, and it is bounded so it can never approach the limit of whatever
  it is actually running inside: not just the machine's own ceiling, but any
  tighter limit set anywhere above the job, since the tightest one is what really
  applies. On a small install the job gets less rather than being handed a cap
  equal to all the memory there is, which would protect nothing. On a machine too
  small to index at all the job still dies, which is the right outcome — better a
  stale index than a machine that falls over.
