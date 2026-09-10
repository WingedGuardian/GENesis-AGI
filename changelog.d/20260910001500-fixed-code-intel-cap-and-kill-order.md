- **The code indexer no longer gets killed for being the right size.** Indexing
  this repository needs about 2.8 GB, but the job was capped at 2 GB — so it was
  killed partway through, every time, and the code intelligence it produces went
  stale without anything obviously failing. The cap now comes from that
  measurement, and it is bounded so it can never approach the machine's own
  limit: on a small install the job gets less rather than being handed a cap
  equal to all the memory there is, which would protect nothing. On a machine
  too small to index at all the job still dies, which is the right outcome —
  better a stale index than a machine that falls over.
- **If memory does run out, the indexer is now the thing chosen to die.** It is
  a batch job that can simply run again, unlike a session holding your in-flight
  work. A value supplied by hand is checked properly first: one written with a
  leading zero used to be read as a different number entirely, and an absurdly
  large one used to wrap around and quietly become the *least* protective
  setting.
