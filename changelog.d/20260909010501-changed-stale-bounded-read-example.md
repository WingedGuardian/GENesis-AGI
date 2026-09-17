- **Corrected a stale example about bounded output.** The development guide
  cited the cap on a subprocess's captured output as an example of a size limit
  that cuts without saying so. That limit now reports the true total alongside
  the bytes it kept, so the guide no longer describes it as silent — but it
  also records why the fix is not yet complete end to end: the notice is added
  at the tail of a large field that every later reader trims well before it,
  so a declaration still has to survive its consumers to count as one.
